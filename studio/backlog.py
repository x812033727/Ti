"""持久任務 backlog —— 跨場次任務佇列（autopilot 與專案持續改良迴圈共用）。

存成單一 JSON 檔（read-modify-write，以檔案鎖序列化），讓自動迴圈與網頁 API 兩個
程序都能安全增減。任務狀態：pending → in_progress → done | failed | parked
（parked＝分診歸檔的長期失敗任務，不再被 next_pending 撿走，但保留紀錄可稽核）。

預設操作 autopilot 的全域 backlog（config.AUTOPILOT_STATE_DIR）；所有公開函式都接受
keyword-only 的 `state_dir`，傳入即操作該目錄下的 backlog（專案持續改良迴圈用——每個
專案一份獨立佇列，互不干擾）。

純檔案 IO、與 LLM 解耦，方便單元測試（測試時用 TI_AUTOPILOT_STATE_DIR 或 state_dir
指向 tmp）。
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import re
import time
from pathlib import Path

from . import config, secure_write

# 唯一 choke point：backlog.json 寫入經 secure_write.secure_write_root。
# module-level alias 兼顧可被測試 monkeypatch。
secure_write_root = secure_write.secure_write_root

# merging＝PR 已開、GitHub 原生 auto-merge 已掛上、等 CI 綠背景合併（完成率第三輪修法二B）。
# 非終局：completion_stats 天然排除；_recover_stale_in_progress 只掃 in_progress 不誤傷；
# 由 autopilot._maybe_reconcile_open_prs 週期收斂成 done / pending / failed。
VALID_STATUS = ("pending", "in_progress", "merging", "done", "failed", "parked")

# 任務類型：功能缺口 / 缺陷 / 一般改良。來源外的值一律正規化成 improvement。
VALID_TYPES = ("feature", "bug", "improvement")

# 優先級 P0（必須）~ P2（加分）。舊資料無此欄位時以 P1 解讀，排序行為與先前 FIFO 一致。
DEFAULT_PRIORITY = 1


def _clamp_priority(priority) -> int:
    """夾到 0..2；不可解析時回預設 P1（解析失敗不該擋任務入列）。"""
    try:
        return min(2, max(0, int(priority)))
    except (TypeError, ValueError):
        return DEFAULT_PRIORITY


def _norm_type(item_type) -> str:
    t = str(item_type or "").strip().lower()
    return t if t in VALID_TYPES else "improvement"


def _dir(state_dir: Path | None) -> Path:
    return state_dir if state_dir is not None else config.AUTOPILOT_STATE_DIR


def _path(state_dir: Path | None) -> Path:
    return _dir(state_dir) / "backlog.json"


def _lock_path(state_dir: Path | None) -> Path:
    return _dir(state_dir) / "backlog.lock"


@contextlib.contextmanager
def _locked(state_dir: Path | None):
    """以獨立 lock 檔序列化 read-modify-write，跨程序安全。"""
    _dir(state_dir).mkdir(parents=True, exist_ok=True)
    lock = _lock_path(state_dir).open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


# 唯讀路徑的 mtime 快取：list_tasks/counts/next_pending/completion_stats 每次全量
# parse(生產 ~210KB/377 筆)太重,且 /api/autopilot 每次輪詢連打三發。以
# (st_mtime_ns, st_size) 雙訊號判新鮮(同秒連寫靠 ns+size 分辨);跨程序(web 與
# autopilot 各自行程)各自快取、各自以檔案訊號失效,一致性由磁碟為準。
# 契約:快取物件是**唯讀共享**——唯讀消費端不得就地變更;寫路徑(_locked 內)一律
# mutable=True 繞過快取直讀最新,_save 後同步刷新,天然拿不到共享物件。
_read_cache: dict[str, tuple[tuple[int, int], dict]] = {}


def _stat_sig(p: Path) -> tuple[int, int] | None:
    try:
        st = p.stat()
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def _load(state_dir: Path | None, *, mutable: bool = False) -> dict:
    p = _path(state_dir)
    if not p.is_file():
        return {"seq": 0, "tasks": []}
    key = str(p)
    if not mutable:
        sig = _stat_sig(p)
        cached = _read_cache.get(key)
        if sig is not None and cached is not None and cached[0] == sig:
            return cached[1]
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"seq": 0, "tasks": []}
    if not mutable:
        sig = _stat_sig(p)
        if sig is not None:
            _read_cache[key] = (sig, data)
    return data


def _save(data: dict, state_dir: Path | None) -> None:
    # 由各公開函式在 _locked() 範圍內呼叫；secure_write_root 的內部 tmp+rename 在 flock
    # 保護下執行，無 TOCTOU。原子 + symlink 防護 + 依 TI_REQUIRE_CHOWN 驗證 root owner。
    _dir(state_dir).mkdir(parents=True, exist_ok=True)
    secure_write_root(
        _path(state_dir),
        json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"),
    )
    # 寫後刷新唯讀快取:寫方持有的 data 即最新內容(呼叫端慣例=改完即 _save,不再變更)。
    sig = _stat_sig(_path(state_dir))
    if sig is not None:
        _read_cache[str(_path(state_dir))] = (sig, data)


def add(
    title: str,
    detail: str = "",
    source: str = "seed",
    *,
    state_dir: Path | None = None,
    priority: int = DEFAULT_PRIORITY,
    item_type: str = "improvement",
    effort: str = "",
    gen: int = 0,
) -> dict | None:
    """新增一筆 pending 任務，回傳該任務；title 為空或重複則回 None。

    priority（0=P0 必須 ~ 2=P2 加分，越小越優先）與 item_type/effort 為可選的
    結構化欄位；舊呼叫端不傳即取預設值，行為不變。

    gen＝衍生代數（討論 discovered followup 的血緣深度，seed/manual/eval=0，父任務的 followup=父+1）；
    供 autopilot 對「單一任務衍生扇出/血緣深度」設上限，封住 discovered 迴圈灌水（完成率修法②）。
    只在 >0 時落欄位，保持既有任務 dict 形狀不變、與現存 backlog 相容（讀取端一律 `.get("gen", 0)`）。
    """
    title = (title or "").strip()
    if not title:
        return None
    with _locked(state_dir):
        data = _load(state_dir, mutable=True)
        if _is_duplicate(data["tasks"], title):
            return None
        data["seq"] += 1
        task = {
            "id": data["seq"],
            "title": title,
            "detail": (detail or "").strip(),
            "status": "pending",
            "source": source,
            "priority": _clamp_priority(priority),
            "type": _norm_type(item_type),
            "effort": (effort or "").strip(),
            "attempts": 0,
            "created_at": time.time(),
            "updated_at": time.time(),
            "session_id": None,
        }
        if gen:
            task["gen"] = int(gen)
        data["tasks"].append(task)
        _save(data, state_dir)
        return task


def add_many(
    titles: list[str],
    source: str = "discovered",
    *,
    state_dir: Path | None = None,
    gen: int = 0,
) -> int:
    """批次新增（去重），回傳實際新增數。gen 見 `add`（衍生代數，供扇出/血緣上限）。"""
    n = 0
    for t in titles:
        if add(t, source=source, state_dir=state_dir, gen=gen):
            n += 1
    return n


def add_items(
    items: list[dict],
    source: str = "discovered",
    *,
    state_dir: Path | None = None,
    gen: int = 0,
) -> int:
    """批次新增結構化任務（{title, detail?, priority?, type?, effort?}），回傳實際新增數。

    與 add_many 並列：消費端解析出優先級/類型時走這裡，純標題清單仍走 add_many。
    gen 見 `add`（衍生代數，供扇出/血緣上限）。
    """
    n = 0
    for it in items:
        if add(
            it.get("title", ""),
            it.get("detail", ""),
            source=source,
            state_dir=state_dir,
            priority=it.get("priority", DEFAULT_PRIORITY),
            item_type=it.get("type", "improvement"),
            effort=it.get("effort", ""),
            gen=gen,
        ):
            n += 1
    return n


def _is_duplicate(tasks: list[dict], title: str) -> bool:
    """同標題且仍 pending/in_progress/merging 視為重複，避免回饋迴圈讓 backlog 暴增。"""
    return any(
        t["title"].strip() == title and t["status"] in ("pending", "in_progress", "merging")
        for t in tasks
    )


def list_tasks(status: str | None = None, *, state_dir: Path | None = None) -> list[dict]:
    data = _load(state_dir)
    tasks = data["tasks"]
    if status:
        tasks = [t for t in tasks if t["status"] == status]
    return tasks


def next_pending(*, state_dir: Path | None = None) -> dict | None:
    """取優先級最高（P0 先）、同級內最早建立、仍 pending 的任務（不改狀態）。

    舊資料無 priority 欄位時以 P1 解讀，故純舊資料下順序與先前 FIFO 完全一致。
    """
    pend = [t for t in _load(state_dir)["tasks"] if t["status"] == "pending"]
    pend.sort(key=lambda t: (t.get("priority", DEFAULT_PRIORITY), t["created_at"]))
    return pend[0] if pend else None


def set_status(
    task_id: int, status: str, *, state_dir: Path | None = None, **fields
) -> dict | None:
    """更新任務狀態與其他欄位（session_id 等）；in_progress 時 attempts +1。"""
    if status not in VALID_STATUS:
        raise ValueError(f"invalid status: {status}")
    with _locked(state_dir):
        data = _load(state_dir, mutable=True)
        for t in data["tasks"]:
            if t["id"] == task_id:
                t["status"] = status
                if status == "in_progress":
                    t["attempts"] = t.get("attempts", 0) + 1
                t["updated_at"] = time.time()
                t.update(fields)
                _save(data, state_dir)
                return t
        return None


def annotate(task_id: int, note: str, *, state_dir: Path | None = None) -> dict | None:
    """只補 note（與 updated_at），不動 status/attempts。

    與 set_status 區隔的原因：set_status(id, "in_progress") 會 attempts +1——為了補一句
    稽核註記而重呼叫會燒掉閘門重試額度。分診/稽核類「純備註」一律走本函式。
    """
    with _locked(state_dir):
        data = _load(state_dir, mutable=True)
        for t in data["tasks"]:
            if t["id"] == task_id:
                t["note"] = note
                t["updated_at"] = time.time()
                _save(data, state_dir)
                return t
        return None


def counts(*, state_dir: Path | None = None) -> dict:
    c = {s: 0 for s in VALID_STATUS}
    for t in _load(state_dir)["tasks"]:
        c[t["status"]] = c.get(t["status"], 0) + 1
    return c


def completion_stats(window: int = 50, *, state_dir: Path | None = None) -> dict:
    """近 window 筆『終局』任務(done/failed)的完成率，供看板顯示真實近況。

    刻意只納 done+failed 為分母,排除:
      - `parked`：永不清除的歸檔態(逾時待拆分、no-op、14 天陳年 failed),留在分母會把
        暫時性損失長期往下拖(見完成率診斷);park 也非「當下一次派工的成敗」。
      - `pending`/`in_progress`：尚未終局。
    再取 updated_at 最近的 window 筆——終身數字會被早期歷史灌水,近窗才反映現況。
    window<=0 表示不設窗(全部終局任務)。rate 於無終局任務時為 None(前端顯示「—」)。
    """
    terminal = [t for t in _load(state_dir)["tasks"] if t.get("status") in ("done", "failed")]
    terminal.sort(key=lambda t: t.get("updated_at", 0), reverse=True)
    recent = terminal[:window] if window > 0 else terminal
    done = sum(1 for t in recent if t["status"] == "done")
    total = len(recent)
    return {
        "window": window,
        "done": done,
        "failed": total - done,
        "total": total,
        "rate": (done / total) if total else None,
    }


def overview(window: int = 50, *, state_dir: Path | None = None) -> dict:
    """一次載入同時給 counts+completion(供 /api/autopilot——舊寫法每次輪詢連打三發全量
    parse)。口徑與 counts()/completion_stats() 逐字段等價(守護測試以等價 oracle 釘死)。"""
    tasks = _load(state_dir)["tasks"]
    c = {st: 0 for st in VALID_STATUS}
    for t in tasks:
        c[t["status"]] = c.get(t["status"], 0) + 1
    terminal = [t for t in tasks if t.get("status") in ("done", "failed")]
    terminal.sort(key=lambda t: t.get("updated_at", 0), reverse=True)
    recent = terminal[:window] if window > 0 else terminal
    done = sum(1 for t in recent if t["status"] == "done")
    total = len(recent)
    completion = {
        "window": window,
        "done": done,
        "failed": total - done,
        "total": total,
        "rate": (done / total) if total else None,
    }
    return {"counts": c, "completion": completion}


def recent_done_titles(limit: int, *, state_dir: Path | None = None) -> set[str]:
    """近期已完成任務的標題集合（取最新 N 筆），供「找問題／自我評估」去重過濾。"""
    if limit <= 0:
        return set()
    done = sorted(
        list_tasks("done", state_dir=state_dir), key=lambda t: t.get("updated_at", 0), reverse=True
    )
    return {t["title"].strip() for t in done[:limit]}


def route_core_changes(items: list[dict]) -> int:
    """把判定的核心改動路由到核心 backlog（雙軌路由的單一收斂點），回傳實際路由數。

    `核心改動:` 專指「改 Ti 框架本身」、與專案無關——任何來源（檢討／找問題／單場討論／autopilot）
    都進同一份核心 backlog（省略 state_dir＝預設 config.AUTOPILOT_STATE_DIR，正是 autopilot 在 drain
    的那份），以 source="core" 標記供稽核；由 autopilot 在核心 repo（config.CORE_REPO）的 working
    clone 實作、過閘門、開「獨立 PR」——絕不進專案 backlog／PR。

    路由前過濾近期已完成的同名項目：_is_duplicate 只擋 pending/in_progress、擋不到 done，否則同一條
    核心改動做完後被別場/別輪再次提出時會重複排入、對核心 repo 開重複/空轉的外部 PR（與「找問題」
    _discover 的去重一致）。AUTOPILOT_EVAL_MEMORY=0 時回空集合＝不過濾，向後相容。
    """
    items = items or []
    if not items:
        return 0
    done = recent_done_titles(config.AUTOPILOT_EVAL_MEMORY)
    items = [c for c in items if c.get("title", "").strip() not in done]
    return add_items(items, source="core") if items else 0


# --- failed 分診（確定性規則、無 LLM）--------------------------------------

# 基礎設施型失敗的 note 特徵：環境／網路／部署互斥等非任務本身缺陷，值得自動重試。
# 對應來源：runner/_run 逾時（「逾時」）、autopilot task timeout（timeout）、provider
# 額度/連線（unreachable / provider unavailable）、deploy.redeploy（重佈失敗／另一個部署進行中）。
INFRA_FAILURE_RE = re.compile(
    r"逾時|timeout|unreachable|provider unavailable|重佈失敗|另一個部署進行中",
    re.IGNORECASE,
)

# 單次分診至多退回 pending 的筆數（取 updated_at 最近者），避免一次灌爆佇列；
# 超出者維持 failed，下次分診再處理。
TRIAGE_RETRY_MAX = 10

# 非基礎設施型 failed 滿此秒數仍未被處理即歸檔 parked（14 天）。
TRIAGE_PARK_AFTER_S = 14 * 86400


def triage_failed(*, state_dir: Path | None = None) -> dict:
    """failed 任務的確定性分診（純規則、無 LLM），回傳 {"retried": n, "parked": m}。

    規則（單次 _locked 內完成，跨程序安全）：
      1. legacy 非法狀態 "cancelled"（歷史殘留，set_status 會 reject）一律直接改 dict
         洗白成 parked。
      2. failed 且 note 命中 INFRA_FAILURE_RE 且 attempts < AUTOPILOT_TASK_MAX_ATTEMPTS
         → 重置 attempts、退回 pending 重試；單次至多 TRIAGE_RETRY_MAX 筆（取最近更新者）。
      3. 其餘 failed（含「連續 N 次未過，放棄」「討論未達完成」等任務本身缺陷）滿
         TRIAGE_PARK_AFTER_S（14 天）→ 歸檔 parked，不再佔據失敗清單；未滿則維持
         failed 等待人工或後續分診。
    """
    retried = parked = 0
    now = time.time()
    with _locked(state_dir):
        data = _load(state_dir, mutable=True)
        for t in data["tasks"]:
            if t.get("status") == "cancelled":
                # legacy 非法值：set_status 會 raise，故在鎖內直接改 dict 洗白。
                t["status"] = "parked"
                t["updated_at"] = now
                t["note"] = "[triage] legacy status cancelled 轉 parked"
                parked += 1
        failed = [t for t in data["tasks"] if t.get("status") == "failed"]
        infra = [
            t
            for t in failed
            if INFRA_FAILURE_RE.search(t.get("note") or "")
            and int(t.get("attempts") or 0) < config.AUTOPILOT_TASK_MAX_ATTEMPTS
        ]
        infra.sort(key=lambda t: t.get("updated_at", 0), reverse=True)
        retry_ids = {t["id"] for t in infra[:TRIAGE_RETRY_MAX]}
        for t in failed:
            if t["id"] in retry_ids:
                t["status"] = "pending"
                t["attempts"] = 0
                t["updated_at"] = now
                t["note"] = f"[triage] 基礎設施型失敗，重置重試；{(t.get('note') or '')[:300]}"
                retried += 1
            elif now - float(t.get("updated_at") or 0) >= TRIAGE_PARK_AFTER_S:
                t["status"] = "parked"
                t["updated_at"] = now
                parked += 1
        if retried or parked:
            _save(data, state_dir)
    return {"retried": retried, "parked": parked}
