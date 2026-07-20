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
VALID_RISKS = ("low", "medium", "high-reversible", "irreversible", "unknown")

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


def _norm_risk(risk) -> str:
    value = str(risk or "").strip().lower()
    return value if value in VALID_RISKS else "unknown"


def _norm_rollback(value) -> dict:
    if not isinstance(value, dict):
        return {}
    return {
        "dry_run": bool(value.get("dry_run")),
        "backup": bool(value.get("backup")),
        "verified": bool(value.get("verified")),
        "scope_limit": str(value.get("scope_limit") or "")[:300],
    }


def _norm_approvals(value) -> list[dict]:
    if not isinstance(value, list):
        return []
    rows: list[dict] = []
    for item in value[:2]:
        if not isinstance(item, dict):
            continue
        rows.append(
            {
                "provider": str(item.get("provider") or "")[:50],
                "diff_sha": str(item.get("diff_sha") or "")[:128],
                "evidence_sha": str(item.get("evidence_sha") or "")[:128],
                "verdict": str(item.get("verdict") or "")[:20],
                "rationale": str(item.get("rationale") or "")[:2000],
            }
        )
    return rows


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
    risk: str = "medium",
    eligible: bool | None = True,
    exclusion_reason: str = "",
    rollback: dict | None = None,
    approval_verdicts: list[dict] | None = None,
    diff_sha: str = "",
    evidence_sha: str = "",
    human_approved: bool = False,
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
    if eligible is False and not (exclusion_reason or "").strip():
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
            # eligible 在任務建立/開工前固定；舊資料缺欄由量測端視為 unknown，絕不灌分母。
            "eligible": bool(eligible) if eligible is not None else "unknown",
            "exclusion_reason": (exclusion_reason or "").strip()[:500],
            "risk": _norm_risk(risk),
            "attempts": 0,
            "created_at": time.time(),
            "updated_at": time.time(),
            "session_id": None,
        }
        if gen:
            task["gen"] = int(gen)
        if rollback:
            task["rollback"] = _norm_rollback(rollback)
        if approval_verdicts:
            task["approval_verdicts"] = _norm_approvals(approval_verdicts)
        if diff_sha:
            task["diff_sha"] = str(diff_sha)[:128]
        if evidence_sha:
            task["evidence_sha"] = str(evidence_sha)[:128]
        if human_approved:
            task["human_approved"] = True
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
            risk=it.get("risk", "medium"),
            eligible=it.get("eligible", True),
            exclusion_reason=it.get("exclusion_reason", ""),
            rollback=it.get("rollback"),
            approval_verdicts=it.get("approval_verdicts"),
            diff_sha=it.get("diff_sha", ""),
            evidence_sha=it.get("evidence_sha", ""),
            # add_items is the batch/discovery entry point used by AI planners.
            # A generated payload must never be able to self-sign an irreversible
            # operation; only the admin-protected single-task API may set this.
            human_approved=False,
        ):
            n += 1
    return n


def _is_duplicate(tasks: list[dict], title: str) -> bool:
    """同標題且仍 pending/in_progress/merging 視為重複，避免回饋迴圈讓 backlog 暴增。"""
    return any(
        t["title"].strip() == title and t["status"] in ("pending", "in_progress", "merging")
        for t in tasks
    )


# 手動操作的合法 action 與各自允許的來源狀態(功能強化 C1)。
# in_progress/merging 不可 park/retry:進行中任務的狀態機由 runner/reconciler 持有,
# 人工改寫會與其收尾 set_status 互相踩踏(merging 更會讓 reconciler 找不到任務收斂 PR)。
_MANUAL_ACTIONS: dict[str, tuple[str, ...]] = {
    "retry": ("failed", "parked"),
    "park": ("pending", "failed"),
    "unpark": ("parked",),
    "priority": ("pending", "in_progress", "merging", "done", "failed", "parked"),
}


def apply_action(
    task_id: int,
    action: str,
    *,
    priority: int | None = None,
    note: str = "",
    state_dir: Path | None = None,
) -> tuple[dict | None, str]:
    """看板手動操作:retry/park/unpark/priority。回 (task, "") 或 (None, 錯誤訊息)。

    整段檢查+變更都在單一 _locked() 內(仿 triage_failed 鎖內改法)——狀態檢查與寫入拆
    兩步會與 autopilot 主迴圈的 set_status 產生 TOCTOU。刻意不重入 set_status:其
    in_progress 時 attempts+1 的語意不適用人工操作;retry/unpark 明確歸零 attempts
    (parked/failed 多為 attempts 燒滿的歸檔,不歸零會立即再判死)。錯誤訊息開頭固定:
    「不支援」→400、「不存在」→404、「不可」→409(routes 據此映射狀態碼)。
    """
    if action not in _MANUAL_ACTIONS:
        return None, f"不支援的 action:{action}"
    if action == "priority" and priority is None:
        return None, "不支援:priority 動作需帶 priority 欄位"
    extra = (note or "").strip()[:500]
    with _locked(state_dir):
        data = _load(state_dir, mutable=True)
        for t in data["tasks"]:
            if t["id"] != task_id:
                continue
            if t["status"] not in _MANUAL_ACTIONS[action]:
                return None, f"不可對 {t['status']} 任務執行 {action}"
            if action == "priority":
                t["priority"] = _clamp_priority(priority)
                if extra:
                    t["note"] = f"[手動] {extra}"
            else:
                target = {"retry": "pending", "park": "parked", "unpark": "pending"}[action]
                t["status"] = target
                if action in ("retry", "unpark"):
                    t["attempts"] = 0
                if action == "park":
                    # 手動歸檔不是澄清票:清殘留 clarify,免得在收件匣死灰復燃(F1 覆審修)。
                    t.pop("clarify", None)
                label = {"retry": "重試", "park": "歸檔", "unpark": "取回"}[action]
                t["note"] = f"[手動] {label}" + (f":{extra}" if extra else "")
            t["updated_at"] = time.time()
            _save(data, state_dir)
            return t, ""
        return None, f"不存在的任務:{task_id}"


def list_tasks(status: str | None = None, *, state_dir: Path | None = None) -> list[dict]:
    data = _load(state_dir)
    tasks = data["tasks"]
    if status:
        tasks = [t for t in tasks if t["status"] == status]
    return tasks


def get(task_id: int, *, state_dir: Path | None = None) -> dict | None:
    """依 id 讀取單一任務；找不到回 None。"""
    for task in _load(state_dir)["tasks"]:
        if task["id"] == task_id:
            return task
    return None


def claim_next(predicate, *, state_dir: Path | None = None) -> dict | None:
    """原子認領:單一 _locked() 內找第一筆滿足 predicate 的 pending 任務並就地標
    in_progress(attempts+1,與 set_status 語意一致),回傳任務或 None。

    為什麼需要:next_pending 只讀不改,認領靠呼叫端事後 set_status——單線無妨,但旁路
    併行線(調查 sideline)與主迴圈同時取任務會 TOCTOU 撿到同一筆。predicate 在鎖內
    執行,須為純函式(不得再進 backlog,否則 flock 重入死鎖)。
    排序與 next_pending 一致(priority 先、同級 created_at 早者先);retry_after
    在未來者跳過(重試冷卻,見 _retry_ready)。
    """
    with _locked(state_dir):
        data = _load(state_dir, mutable=True)
        pend = [t for t in data["tasks"] if t["status"] == "pending" and _retry_ready(t)]
        pend.sort(key=lambda t: (t.get("priority", DEFAULT_PRIORITY), t["created_at"]))
        for t in pend:
            if not predicate(t):
                continue
            t["status"] = "in_progress"
            t["attempts"] = t.get("attempts", 0) + 1
            t["updated_at"] = time.time()
            _save(data, state_dir)
            return t
        return None


def _retry_ready(t: dict) -> bool:
    """重試冷卻閘:retry_after(epoch)在未來的 pending 不揀——立即重抓會把 attempts
    在同一個 provider 劣化窗口內燒光(2026-07-11 09:24 實證)。欄位缺失/非數值＝無冷卻
    (舊資料完全不受影響);到點後自然恢復可揀,無需任何清理。"""
    try:
        return float(t.get("retry_after") or 0) <= time.time()
    except (TypeError, ValueError):
        return True


def next_pending(*, state_dir: Path | None = None) -> dict | None:
    """取優先級最高（P0 先）、同級內最早建立、仍 pending 的任務（不改狀態）。

    舊資料無 priority 欄位時以 P1 解讀，故純舊資料下順序與先前 FIFO 完全一致;
    retry_after 在未來者跳過(重試冷卻,見 _retry_ready)。
    """
    pend = [t for t in _load(state_dir)["tasks"] if t["status"] == "pending" and _retry_ready(t)]
    pend.sort(key=lambda t: (t.get("priority", DEFAULT_PRIORITY), t["created_at"]))
    return pend[0] if pend else None


def set_status(
    task_id: int, status: str, *, state_dir: Path | None = None, **fields
) -> dict | None:
    """更新任務狀態與其他欄位（session_id 等）；in_progress 時 attempts +1。

    clarify 不變量(F1 覆審修):clarify 只在「帶著問題停放」時有效——不帶新 clarify 的
    parked 轉換清掉殘留舊問題,否則答過的票會在日後無關停放時於收件匣死灰復燃。
    (unpark 刻意保留 clarify:_clarify_requirement_section 靠它注入問題+人工回覆。)
    """
    if status not in VALID_STATUS:
        raise ValueError(f"invalid status: {status}")
    with _locked(state_dir):
        data = _load(state_dir, mutable=True)
        for t in data["tasks"]:
            if t["id"] == task_id:
                t["status"] = status
                if status == "in_progress":
                    t["attempts"] = t.get("attempts", 0) + 1
                if status == "parked" and "clarify" not in fields:
                    t.pop("clarify", None)
                t["updated_at"] = time.time()
                t.update(fields)
                _save(data, state_dir)
                return t
        return None


def annotate(
    task_id: int,
    note: str,
    *,
    state_dir: Path | None = None,
    lane: str | None = None,
) -> dict | None:
    """只補 note（可選 lane）與 updated_at，不動 status/attempts。

    與 set_status 區隔的原因：set_status(id, "in_progress") 會 attempts +1——為了補一句
    稽核註記而重呼叫會燒掉閘門重試額度。分診/稽核類「純備註」一律走本函式。
    """
    with _locked(state_dir):
        data = _load(state_dir, mutable=True)
        for t in data["tasks"]:
            if t["id"] == task_id:
                t["note"] = note
                if lane is not None:
                    t["lane"] = lane
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


def route_core_changes(items: list[dict], *, source: str = "core") -> int:
    """把判定的核心改動路由到核心 backlog（雙軌路由的單一收斂點），回傳實際路由數。

    source 預設 "core";improver 意圖差距分析驅動時傳 "intent"(F2)——這是「使用者意圖→
    核心自產→零人工交付」在 core 迴圈唯一可量測的通道(專案 backlog 不進 core audit)。

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
    return add_items(items, source=source) if items else 0


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

# timeout parked note 由 autopilot.py 產生；此 marker 是跨模組解析契約，勿單邊修改。
TIMEOUT_NOTE_PREFIX = "task timeout after"
TIMEOUT_NOTE_RE = re.compile(rf"{re.escape(TIMEOUT_NOTE_PREFIX)}\s+(\d+)s")

# 單次分診至多把 timeout parked 退回 pending 的筆數；獨立於 failed retry 配額。
TRIAGE_UNPARK_MAX = 5

# 非基礎設施型 failed 滿此秒數仍未被處理即歸檔 parked（14 天）。
TRIAGE_PARK_AFTER_S = 14 * 86400

# 「討論未達完成」用罄 failed 的冷卻復活（第五輪 C1）：failed 滿此秒數且從未復活過
# （discussion_revives 欄=0）→ 單次退回 pending 再給一輪機會。此桶佔 failed 48% 且
# LLM 非決定性、換日重跑常會過；復活次數記在任務欄位而非 note（note 每次 set_status
# 會被覆寫，用 note 標記會導致每 24h 無限復活）。
TRIAGE_REVIVE_AFTER_S = 24 * 3600


def triage_failed(*, state_dir: Path | None = None) -> dict:
    """failed/timeout parked 任務的確定性分診（純規則、無 LLM）。

    規則（單次 _locked 內完成，跨程序安全）：
      1. legacy 非法狀態 "cancelled"（歷史殘留，set_status 會 reject）一律直接改 dict
         洗白成 parked。
      2. failed 且 note 命中 INFRA_FAILURE_RE 且 attempts < AUTOPILOT_TASK_MAX_ATTEMPTS
         → 重置 attempts、退回 pending 重試；單次至多 TRIAGE_RETRY_MAX 筆（取最近更新者）。
      2b. failed 且 note 含「討論未達完成」且從未復活（discussion_revives=0）、冷卻滿
         TRIAGE_REVIVE_AFTER_S（24h）且未滿 14 天 → 單次退回 pending 復活（attempts
         歸零、discussion_revives+1 記在任務欄位防無限循環）；與規則 2 共用
         TRIAGE_RETRY_MAX 配額。復活後再失敗 → 不再復活，走規則 3 的 14 天歸檔。
      3. 其餘 failed（含「連續 N 次未過，放棄」等任務本身缺陷）滿
         TRIAGE_PARK_AFTER_S（14 天）→ 歸檔 parked，不再佔據失敗清單；未滿則維持
         failed 等待人工或後續分診。
      4. parked 且 note 為 autopilot timeout、park 時 timeout 秒數 < 現行
         AUTOPILOT_TASK_TIMEOUT、且未 timeout_retried/split_done → 退回 pending
         單次重試（attempts 歸零、timeout_retried=True）；單次至多 TRIAGE_UNPARK_MAX
         筆，且不佔 failed retry 配額。
    """
    retried = parked = revived = unparked = 0
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
        discussion = [
            t
            for t in failed
            if t["id"] not in retry_ids
            and "討論未達完成" in (t.get("note") or "")
            and not int(t.get("discussion_revives") or 0)
            # 冷卻滿 24h 才復活;已滿 14 天的陳年失敗直接走規則 3 歸檔(不再折騰)。
            and TRIAGE_REVIVE_AFTER_S <= now - float(t.get("updated_at") or 0) < TRIAGE_PARK_AFTER_S
        ]
        discussion.sort(key=lambda t: t.get("updated_at", 0), reverse=True)
        revive_budget = max(0, TRIAGE_RETRY_MAX - len(retry_ids))
        revive_ids = {t["id"] for t in discussion[:revive_budget]}
        for t in failed:
            if t["id"] in retry_ids:
                t["status"] = "pending"
                t["attempts"] = 0
                t["updated_at"] = now
                t["note"] = f"[triage] 基礎設施型失敗，重置重試；{(t.get('note') or '')[:300]}"
                retried += 1
            elif t["id"] in revive_ids:
                t["status"] = "pending"
                t["attempts"] = 0
                t["discussion_revives"] = int(t.get("discussion_revives") or 0) + 1
                t["updated_at"] = now
                t["note"] = f"[triage] 討論未收斂冷卻復活（單次）；{(t.get('note') or '')[:300]}"
                revived += 1
            elif now - float(t.get("updated_at") or 0) >= TRIAGE_PARK_AFTER_S:
                t["status"] = "parked"
                t["updated_at"] = now
                parked += 1
        timeout_parked: list[tuple[dict, int]] = []
        for t in data["tasks"]:
            if t.get("status") != "parked" or t.get("timeout_retried") or t.get("split_done"):
                continue
            m = TIMEOUT_NOTE_RE.search(t.get("note") or "")
            if not m:
                continue
            parked_timeout_s = int(m.group(1))
            if parked_timeout_s < int(config.AUTOPILOT_TASK_TIMEOUT):
                timeout_parked.append((t, parked_timeout_s))
        timeout_parked.sort(key=lambda pair: pair[0].get("updated_at", 0), reverse=True)
        for t, parked_timeout_s in timeout_parked[:TRIAGE_UNPARK_MAX]:
            old_note = t.get("note") or ""
            t["status"] = "pending"
            t["attempts"] = 0
            t["timeout_retried"] = True
            t["updated_at"] = now
            t["note"] = (
                f"[triage] timeout 上限已由 {parked_timeout_s}s 調高至 "
                f"{config.AUTOPILOT_TASK_TIMEOUT}s，退回重試；{old_note[:300]}"
            )
            unparked += 1
        if retried or parked or revived or unparked:
            _save(data, state_dir)
    return {"retried": retried, "parked": parked, "revived": revived, "unparked": unparked}
