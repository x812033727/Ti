"""跨場次教訓庫 —— 工作室的長期記憶。

每場討論的檢討會蒸餾出可重用的「教訓」（踩過的坑、有效做法、技術選型結論），
持久化到單一 JSON 檔；下次新討論開場時注入 PM 拆解，讓工作室跨場次自我加強——
避免重蹈覆轍、善用既有結論。

存法與 backlog 一致：單一 JSON 檔 + 檔案鎖序列化 read-modify-write，讓多個 session
程序安全增寫。純檔案 IO、與 LLM 解耦，方便單元測試（測試時用 TI_LESSONS_FILE 指向 tmp）。
"""

from __future__ import annotations

import contextlib
import difflib
import fcntl
import json
import math
import re
import time
from pathlib import Path

from . import config

# 檔案最多保留幾筆（由新到舊截斷），封住長跑下只增不減。注入時另以 LESSONS_MAX 取最新 N 筆。
_MAX_STORE = 500
# investigation=調查分流管線的結論沉澱(2026-07-10 修:autopilot 以 source="investigation"
# 呼叫 add_many,但白名單漏列 → ValueError 被呼叫端 suppress 吞掉,調查結論從未真正入庫)。
_VALID_SOURCES = {"retro", "vote", "appraisal", "investigation", "intervention"}

# 目前工作目錄沒有既有 lessons.json，可直接校準的實庫為空；因此用 repo 既有 tests/docs 的教訓句型
# 抽樣校準：明顯近似句 ratio 0.909~0.973，跨主題句 ratio 0.143~0.571。取 0.90 可擋表層
# 近重複，又不把「同領域但不同教訓」合併。固定模板 vote 由接線端傳 exact_only=True 避免前綴墊高誤擋。
_FUZZY_DUP_RATIO = 0.90


def _path() -> Path:
    return config.LESSONS_FILE


def _lock_path() -> Path:
    return config.LESSONS_FILE.with_suffix(".lock")


@contextlib.contextmanager
def _locked():
    """以獨立 lock 檔序列化 read-modify-write，跨程序安全。"""
    _path().parent.mkdir(parents=True, exist_ok=True)
    lock = _lock_path().open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


# 唯讀 mtime 快取(同 backlog 範式):lessons.json 生產 ~268KB,context()/all_lessons()
# 每場注入都全量 parse。寫路徑 mutable=True 繞過、_save 後刷新;快取物件唯讀共享。
_read_cache: dict[str, tuple[tuple[int, int], dict]] = {}


def _stat_sig(p: Path) -> tuple[int, int] | None:
    try:
        st = p.stat()
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def _load(*, mutable: bool = False) -> dict:
    p = _path()
    if not p.is_file():
        return {"lessons": []}
    key = str(p)
    if not mutable:
        sig = _stat_sig(p)
        cached = _read_cache.get(key)
        if sig is not None and cached is not None and cached[0] == sig:
            return cached[1]
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("lessons"), list):
            if not mutable:
                sig = _stat_sig(p)
                if sig is not None:
                    _read_cache[key] = (sig, data)
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return {"lessons": []}


def _save(data: dict) -> None:
    _path().parent.mkdir(parents=True, exist_ok=True)
    tmp = _path().with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(_path())
    sig = _stat_sig(_path())
    if sig is not None:
        _read_cache[str(_path())] = (sig, data)


def _is_fuzzy_duplicate(text: str, existing: list[str]) -> bool:
    """以標準庫做表層近似去重；不嘗試語意 paraphrase。"""
    nums = set(re.findall(r"\d+", text))
    for old in existing:
        # 現有資料常用「第 0 條/第 1 條」這類編號測試不同教訓；數字不同時不做模糊合併。
        if nums != set(re.findall(r"\d+", old)):
            continue
        if difflib.SequenceMatcher(None, text, old, autojunk=False).ratio() >= _FUZZY_DUP_RATIO:
            return True
    return False


def add_many(
    texts: list[str],
    *,
    session_id: str = "",
    requirement: str = "",
    scope: str = "global",
    source: str = "retro",
    exact_only: bool = False,
) -> int:
    """批次新增教訓（對既有內容去重），回傳實際新增數。

    去重預設採「全文完全相符 + difflib 表層近似」：同一句或幾乎同文的教訓只留一筆。
    exact_only=True 時只做全文比對，供固定模板但結論可不同的來源使用。
    scope 預設 "global"（可跨專案重用）；傳入專案 id 則為該專案專屬教訓（見 _scope_ok）。
    source 可為 retro/vote/appraisal，只落檔供追溯，不參與挑選；舊資料無此鍵時讀取端無感。
    新筆附 scope 與 use_count（被注入選中時 +1，供未來淘汰排序）；舊資料無此鍵時讀取端取預設。
    """
    if source not in _VALID_SOURCES:
        raise ValueError(f"invalid lesson source: {source}")
    cleaned = [t.strip() for t in texts if t and t.strip()]
    if not cleaned:
        return 0
    with _locked():
        data = _load(mutable=True)
        existing_texts = [
            text for item in data["lessons"] if (text := item.get("text", "").strip())
        ]
        existing = set(existing_texts)
        n = 0
        for text in cleaned:
            if text in existing or (not exact_only and _is_fuzzy_duplicate(text, existing_texts)):
                continue
            data["lessons"].append(
                {
                    "text": text,
                    "session_id": session_id,
                    "requirement": (requirement or "")[:200],
                    "created_at": time.time(),
                    "scope": scope,
                    "source": source,
                    "use_count": 0,
                }
            )
            existing_texts.append(text)
            existing.add(text)
            n += 1
        if n:
            # 只保留最新 _MAX_STORE 筆（依出現序，新的在尾端）。
            data["lessons"] = data["lessons"][-_MAX_STORE:]
            _save(data)
        return n


def _scope_ok(item: dict, scope: str) -> bool:
    """教訓是否在所求 scope 內可選：global 永遠可選；非 global 僅當 scope 相符才可選。

    舊資料無 scope 鍵時視為 global（零遷移）。scope="" 預設＝只取 global，行為與現狀逐字相同。
    """
    item_scope = item.get("scope", "global")
    return item_scope == "global" or item_scope == scope


def recent(limit: int, *, scope: str = "") -> list[dict]:
    """取最新 limit 筆教訓（由新到舊，依 scope 過濾）。limit <= 0 回空清單。"""
    if limit <= 0:
        return []
    items = [it for it in _load()["lessons"] if _scope_ok(it, scope)]
    return list(reversed(items))[:limit]


def _tokens(text: str) -> set[str]:
    """中英混合輕量斷詞：ASCII 詞 + 中文字元 bigram。

    不引入任何斷詞/embedding 依賴；bigram 對中文的主題比對已足夠
    （「無人機」→ {無人, 人機} 不會撞上「網站後台」的任何 bigram）。
    """
    text = text.lower()
    words = set(re.findall(r"[a-z0-9_]+", text))
    han = re.findall(r"[一-鿿]", text)
    bigrams = {a + b for a, b in zip(han, han[1:], strict=False)}
    return words | bigrams


def relevant(limit: int, requirement: str, *, scope: str = "") -> list[dict]:
    """取與需求最相關的 limit 筆教訓（IDF 加權重疊分數降冪、同分新者優先）。

    做多種產品後教訓庫會混雜（無人機的坑不該注入網站任務）——按相關性挑選而非
    「最新 N 筆」。token 以庫內文件頻率做 IDF 加權：「做一」「一個」這類滿庫都是的
    泛用詞自動降權，主題詞（「無人」「人機」）自然勝出，無需維護停用詞表。
    完全無相關（全部 0 分）時回空清單，由呼叫端退回最新 N 筆。依 scope 過濾（見 _scope_ok）。
    """
    if limit <= 0:
        return []
    items = [it for it in _load()["lessons"] if _scope_ok(it, scope)]
    q = _tokens(requirement)
    if not q or not items:
        return []
    toks = [_tokens(f"{it.get('text', '')} {it.get('requirement', '')}") for it in items]
    df: dict[str, int] = {}
    for lt in toks:
        for t in lt:
            df[t] = df.get(t, 0) + 1
    n = len(items)

    def _score(lt: set[str]) -> float:
        return sum(math.log(1 + n / df[t]) for t in q & lt)

    scored = [(s, it) for it, lt in zip(items, toks, strict=True) if (s := _score(lt)) > 0]
    scored.sort(key=lambda p: (p[0], p[1].get("created_at", 0)), reverse=True)
    return [it for _, it in scored[:limit]]


def _bump_use_count(texts: list[str]) -> None:
    """把被注入選中的教訓 use_count +1（鎖內 read-modify-write）；空清單即 no-op。

    與挑選邏輯解耦：先挑後記，挑選本身不受 use_count 影響（不過度設計）。供未來淘汰排序用。
    """
    wanted = {t.strip() for t in texts if t and t.strip()}
    if not wanted:
        return
    with _locked():
        data = _load(mutable=True)
        changed = False
        for item in data["lessons"]:
            if item.get("text", "").strip() in wanted:
                item["use_count"] = item.get("use_count", 0) + 1
                changed = True
        if changed:
            _save(data)


def context(limit: int | None = None, requirement: str = "", scope: str = "") -> str:
    """組成要注入 PM 拆解 prompt 的教訓區塊；停用、無教訓或 limit<=0 時回 ""。

    有給 requirement 時優先按相關性挑選（避免跨領域教訓互相污染），
    完全無相關或未給需求則退回「最新 N 筆」（原行為）。依 scope 過濾（預設只取 global）。
    被選中的教訓 use_count +1（供未來淘汰排序）。
    """
    if not config.LESSONS_ENABLED:
        return ""
    cap = config.LESSONS_MAX if limit is None else limit
    rows = relevant(cap, requirement, scope=scope) if requirement.strip() else []
    picked_by_relevance = bool(rows)
    if not rows:
        rows = recent(cap, scope=scope)
    if not rows:
        return ""
    texts = [r.get("text", "") for r in rows if r.get("text", "").strip()]
    _bump_use_count(texts)
    body = "\n".join(f"- {t}" for t in texts)
    if not body:
        return ""
    note = "依本次需求相關性挑選" if picked_by_relevance else "最新數筆"
    return (
        f"【跨場次教訓庫（過往各場討論檢討蒸餾，{note}；請避免重蹈覆轍、善用既有結論）】\n"
        f"{body}\n\n"
    )


def all_lessons() -> list[dict]:
    """回傳全部教訓（依儲存序，舊→新）；供檢視 / 測試。"""
    return list(_load()["lessons"])


_DISTILL_SYSTEM = (
    "你是教訓庫的維護者。下面是工作室跨場次累積的『教訓』條目（可能有語意重複、過時或冗長）。"
    "請把相近的合併成一句、刪掉過時或無用的、保留仍有價值的，產出一份精簡、不重複的教訓清單。"
    "嚴格規則：只輸出教訓行，每行格式固定為 `教訓: <一句精簡、可重用的經驗>`，"
    "不要任何前言、編號或解說。輸出數量必須少於輸入數量（這是去重蒸餾，不是改寫）。"
)


def _parse_distilled(text: str) -> list[str]:
    """從 LLM 蒸餾輸出抽出 `教訓: ...` 行（不設 5 條上限，與 orchestrator.parse_lessons 區隔）。"""
    return [
        m.strip() for m in re.findall(r"^\s*教訓\s*[:：]\s*(.+)$", text or "", re.M) if m.strip()
    ]


async def distill(*, session_id: str = "", cwd=None) -> int:
    """用一次 LLM 把 global 教訓語意去重蒸餾，回傳淘汰筆數（0=未執行/無變化）。

    雙閘低頻：庫內 global 筆數 ≥ THRESHOLD 且距上次蒸餾 ≥ INTERVAL 才跑。資料安全為核心——
    LLM 失敗/離線（complete_once 回 ""）、輸出解析 0 筆、筆數未減少、或疑似大規模誤刪
    （< 快照 ×20%）一律保留原庫回 0；絕不讓壞輸出清空長期記憶。無 LLM 時行為與現行 FIFO 相同。
    呼叫端零判斷（全部閘與防呆在此）。project-scope 教訓不參與蒸餾、原樣保留。
    """
    if not (config.LESSONS_ENABLED and config.LESSONS_DISTILL):
        return 0
    # 1) 鎖內讀快照 + 前置閘
    with _locked():
        data = _load(mutable=True)
        last = data.get("meta", {}).get("last_distill_at", 0)
        snapshot = [
            it.get("text", "")
            for it in data["lessons"]
            if it.get("scope", "global") == "global" and it.get("text", "").strip()
        ]
        if len(snapshot) < config.LESSONS_DISTILL_THRESHOLD:
            return 0
        if time.time() - last < config.LESSONS_DISTILL_INTERVAL:
            return 0

    # 2) 鎖外呼叫 LLM（永不 raise；離線/無憑證回 ""）
    from . import providers

    user = "\n".join(f"{i}. {t}" for i, t in enumerate(snapshot, start=1))
    out = await providers.complete_once(
        system=_DISTILL_SYSTEM,
        user=user,
        session_id=f"{session_id}:distill",
        cwd=cwd,
        timeout=120.0,
    )
    distilled = _parse_distilled(out)

    # 3) 資料安全閘（任一命中即保留原庫）
    n_snap = len(snapshot)
    floor = max(1, int(n_snap * 0.2))
    if not distilled or len(distilled) >= n_snap or len(distilled) < floor:
        return 0

    # 4) 套用（重新進鎖；防併發重複套用：last_distill_at 變動代表他人已蒸餾過）
    with _locked():
        data = _load(mutable=True)
        if data.get("meta", {}).get("last_distill_at", 0) != last:
            return 0
        snap_set = set(snapshot)
        # 快照後其他 session 新增的 global 筆（text 不在快照集合）一併保留，不被蒸餾蓋掉。
        new_global = [
            it
            for it in data["lessons"]
            if it.get("scope", "global") == "global" and it.get("text", "") not in snap_set
        ]
        project_items = [it for it in data["lessons"] if it.get("scope", "global") != "global"]
        now = time.time()
        distilled_items = [
            {
                "text": t,
                "session_id": f"{session_id}:distill",
                "requirement": "",
                "created_at": now,
                "scope": "global",
                "source": "retro",
                "use_count": 0,
            }
            for t in distilled
        ]
        data["lessons"] = (distilled_items + new_global + project_items)[-_MAX_STORE:]
        meta = data.get("meta", {})
        meta["last_distill_at"] = now
        data["meta"] = meta
        _save(data)
        return n_snap - len(distilled_items)
