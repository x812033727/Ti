"""任務級反思記憶 —— 補主迴圈「只帶上一輪原文回饋」的缺口。

每輪失敗時，把 QA／高級工程師的意見蒸餾成一段精簡反思（見 reflexion.py），存進
per-session 的 JSONL；後續輪次／huddle 重試前，依任務撈回先前輪次的反思 prepend 進工程師
context，讓同一任務跨輪累積經驗、不重蹈覆轍。

職責邊界：本模組只管「同一 session 內、同一任務跨輪」的短期記憶；跨場次的長期教訓仍歸
lessons.py（兩者不重疊）。存法沿用 lessons.py：單檔 + fcntl 檔案鎖序列化 append，跨程序安全
（多 session／多 uvicorn worker／並行 lane 皆可安全增寫）。純檔案 IO、與 LLM 解耦，易測。

移植自 ti-studio 自我進步交付的 memory.py（去重保最新 + recent_n + token 預算 + 預算為 0
回空守衛 + 0600 權限），把 threading.Lock 換成跨程序的 fcntl 檔案鎖。
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import time
from pathlib import Path

from . import config

# 約略 token 估算：中文偏密，保守用 2.5 char/token（注入端只需「夠用的上界」防爆預算，非精準）。
_CHARS_PER_TOKEN = 2.5

DEFAULT_HEADER = "【過往反思（本任務先前輪次蒸餾，請避免重蹈覆轍）】"


def _safe_id(session_id: str) -> str:
    safe = "".join(c for c in session_id if c.isalnum() or c in "-_")
    return safe or "default"


def memory_path(session_id: str) -> Path:
    return config.HISTORY_ROOT / f"{_safe_id(session_id)}.memory.jsonl"


def _lock_path(session_id: str) -> Path:
    return config.HISTORY_ROOT / f"{_safe_id(session_id)}.memory.lock"


@contextlib.contextmanager
def _locked(session_id: str):
    """以獨立 lock 檔序列化 append，跨程序／並行 lane 安全。"""
    config.HISTORY_ROOT.mkdir(parents=True, exist_ok=True)
    lock = _lock_path(session_id).open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


def write(
    session_id: str,
    task_id,
    content: str,
    *,
    round_no: int,
    kind: str = "reflection",
    meta: dict | None = None,
) -> bool:
    """附加一筆反思（空白內容略過）。回傳是否實際寫入。"""
    content = (content or "").strip()
    if not content:
        return False
    record = {
        "task_id": task_id,
        "round": round_no,
        "content": content,
        "kind": kind,
        "ts": time.time(),
        "meta": meta,
    }
    line = json.dumps(record, ensure_ascii=False)
    with _locked(session_id):
        path = memory_path(session_id)
        new_file = not path.exists()
        with path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        if new_file:
            # 反思可能含任務敏感資訊，限制檔案權限為 0600（僅擁有者讀寫）。
            with contextlib.suppress(OSError):
                os.chmod(path, 0o600)
    return True


def _read_all(session_id: str) -> list[dict]:
    path = memory_path(session_id)
    if not path.exists():
        return []
    records: list[dict] = []
    # 讀不加鎖（與 append 並發最多讀到尚未寫完的壞行，逐行 try 跳過即可）。
    for raw in path.read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            d = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(d, dict):
            records.append(d)
    return records


def retrieve(
    session_id: str,
    task_id=None,
    *,
    kind: str | None = "reflection",
    before_round: int | None = None,
) -> list[dict]:
    """撈回反思（依寫入序，舊→新）。

    - task_id=None：不依任務過濾（取全部）。
    - before_round：只取 round 小於此值的記錄（供 huddle 重試編號避碰）。
    """
    records = _read_all(session_id)
    if task_id is not None:
        records = [r for r in records if r.get("task_id") == task_id]
    if kind is not None:
        records = [r for r in records if r.get("kind") == kind]
    if before_round is not None:
        records = [r for r in records if (r.get("round") or 0) < before_round]
    return records


def build_context(
    session_id: str,
    task_id,
    *,
    exclude_latest: bool = True,
    recent_n: int | None = None,
    token_budget: int = 800,
    header: str = DEFAULT_HEADER,
) -> str:
    """組出可 prepend 進工程師 prompt 的反思區塊；停用、無記憶或預算為 0 時回 ""。

    步驟：撈回 → （可選）排除最新一筆 → 去重（保最新）→ 取最近 recent_n →
    token 預算內由新到舊納入 → 依時間序輸出，尾帶兩個換行可直接前接後文。

    exclude_latest：最新一筆＝上一輪的反思，其原文已由 _work_task 的 verbatim feedback 帶入，
    預設排除以免重複；huddle 重試（seed 為 huddle 結論、非上一輪 QA 報告）則傳 False 全帶。
    """
    if not config.REFLEXION_ENABLED:
        return ""
    records = retrieve(session_id, task_id)
    if exclude_latest and records:
        records = records[:-1]
    if not records:
        return ""
    cap = config.REFLEXION_MAX if recent_n is None else recent_n
    if cap <= 0:  # 預算為 0＝關閉注入，不可退化成全帶
        return ""

    # 去重：同 content 只留最後一次出現（最新）。
    seen: set[str] = set()
    deduped: list[dict] = []
    for r in reversed(records):
        key = (r.get("content") or "").strip()
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(r)
    deduped = deduped[:cap]  # 目前新→舊，取最近 cap 筆

    # 在 token 預算內由新到舊納入。
    char_budget = int(max(0, token_budget) * _CHARS_PER_TOKEN)
    chosen: list[dict] = []
    used = len(header) + 1
    for r in deduped:
        item_len = len(r["content"]) + 4  # "- " 前綴與換行概估
        if used + item_len > char_budget and chosen:
            break
        chosen.append(r)
        used += item_len
    if not chosen:
        return ""

    chosen.reverse()  # 依時間序（舊→新）讀起來自然
    lines = [header]
    lines.extend(f"- {r['content'].strip()}" for r in chosen)
    return "\n".join(lines) + "\n\n"


def delete(session_id: str) -> None:
    """刪除該 session 的記憶與 lock 檔（供 history GC 回收，避免洩漏）。"""
    for p in (memory_path(session_id), _lock_path(session_id)):
        with contextlib.suppress(OSError):
            if p.exists():
                p.unlink()
