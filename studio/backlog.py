"""持久任務 backlog —— 跨場次任務佇列（autopilot 與專案持續改良迴圈共用）。

存成單一 JSON 檔（read-modify-write，以檔案鎖序列化），讓自動迴圈與網頁 API 兩個
程序都能安全增減。任務狀態：pending → in_progress → done | failed。

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
import time
from pathlib import Path

from . import config
from .secure_write import secure_write_root

VALID_STATUS = ("pending", "in_progress", "done", "failed")

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


def _load(state_dir: Path | None) -> dict:
    p = _path(state_dir)
    if not p.is_file():
        return {"seq": 0, "tasks": []}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"seq": 0, "tasks": []}


def _save(data: dict, state_dir: Path | None) -> None:
    # 由各公開函式在 _locked() 範圍內呼叫；secure_write_root 的內部 tmp+rename 在 flock
    # 保護下執行，無 TOCTOU。原子 + symlink 防護 + 依 TI_REQUIRE_CHOWN 驗證 root owner。
    _dir(state_dir).mkdir(parents=True, exist_ok=True)
    secure_write_root(
        _path(state_dir),
        json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"),
    )


def add(
    title: str,
    detail: str = "",
    source: str = "seed",
    *,
    state_dir: Path | None = None,
    priority: int = DEFAULT_PRIORITY,
    item_type: str = "improvement",
    effort: str = "",
) -> dict | None:
    """新增一筆 pending 任務，回傳該任務；title 為空或重複則回 None。

    priority（0=P0 必須 ~ 2=P2 加分，越小越優先）與 item_type/effort 為可選的
    結構化欄位；舊呼叫端不傳即取預設值，行為不變。
    """
    title = (title or "").strip()
    if not title:
        return None
    with _locked(state_dir):
        data = _load(state_dir)
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
        data["tasks"].append(task)
        _save(data, state_dir)
        return task


def add_many(
    titles: list[str], source: str = "discovered", *, state_dir: Path | None = None
) -> int:
    """批次新增（去重），回傳實際新增數。"""
    n = 0
    for t in titles:
        if add(t, source=source, state_dir=state_dir):
            n += 1
    return n


def add_items(
    items: list[dict], source: str = "discovered", *, state_dir: Path | None = None
) -> int:
    """批次新增結構化任務（{title, detail?, priority?, type?, effort?}），回傳實際新增數。

    與 add_many 並列：消費端解析出優先級/類型時走這裡，純標題清單仍走 add_many。
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
        ):
            n += 1
    return n


def _is_duplicate(tasks: list[dict], title: str) -> bool:
    """同標題且仍 pending/in_progress 視為重複，避免回饋迴圈讓 backlog 暴增。"""
    return any(
        t["title"].strip() == title and t["status"] in ("pending", "in_progress") for t in tasks
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
        data = _load(state_dir)
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


def counts(*, state_dir: Path | None = None) -> dict:
    c = {s: 0 for s in VALID_STATUS}
    for t in _load(state_dir)["tasks"]:
        c[t["status"]] = c.get(t["status"], 0) + 1
    return c


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
