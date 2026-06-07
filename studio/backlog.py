"""持久任務 backlog —— autopilot 的跨場次任務佇列。

存成單一 JSON 檔（read-modify-write，以檔案鎖序列化），讓自動迴圈與網頁 API 兩個
程序都能安全增減。任務狀態：pending → in_progress → done | failed。

純檔案 IO、與 LLM 解耦，方便單元測試（測試時用 TI_AUTOPILOT_STATE_DIR 指向 tmp）。
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import time
from pathlib import Path

from . import config

VALID_STATUS = ("pending", "in_progress", "done", "failed")


def _path() -> Path:
    return config.AUTOPILOT_STATE_DIR / "backlog.json"


def _lock_path() -> Path:
    return config.AUTOPILOT_STATE_DIR / "backlog.lock"


@contextlib.contextmanager
def _locked():
    """以獨立 lock 檔序列化 read-modify-write，跨程序安全。"""
    config.AUTOPILOT_STATE_DIR.mkdir(parents=True, exist_ok=True)
    lock = _lock_path().open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


def _load() -> dict:
    p = _path()
    if not p.is_file():
        return {"seq": 0, "tasks": []}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"seq": 0, "tasks": []}


def _save(data: dict) -> None:
    config.AUTOPILOT_STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _path().with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(_path())


def add(title: str, detail: str = "", source: str = "seed") -> dict | None:
    """新增一筆 pending 任務，回傳該任務；title 為空或重複則回 None。"""
    title = (title or "").strip()
    if not title:
        return None
    with _locked():
        data = _load()
        if _is_duplicate(data["tasks"], title):
            return None
        data["seq"] += 1
        task = {
            "id": data["seq"],
            "title": title,
            "detail": (detail or "").strip(),
            "status": "pending",
            "source": source,
            "attempts": 0,
            "created_at": time.time(),
            "updated_at": time.time(),
            "session_id": None,
        }
        data["tasks"].append(task)
        _save(data)
        return task


def add_many(titles: list[str], source: str = "discovered") -> int:
    """批次新增（去重），回傳實際新增數。"""
    n = 0
    for t in titles:
        if add(t, source=source):
            n += 1
    return n


def _is_duplicate(tasks: list[dict], title: str) -> bool:
    """同標題且仍 pending/in_progress 視為重複，避免回饋迴圈讓 backlog 暴增。"""
    return any(
        t["title"].strip() == title and t["status"] in ("pending", "in_progress")
        for t in tasks
    )


def list_tasks(status: str | None = None) -> list[dict]:
    data = _load()
    tasks = data["tasks"]
    if status:
        tasks = [t for t in tasks if t["status"] == status]
    return tasks


def next_pending() -> dict | None:
    """取最早建立、仍 pending 的任務（不改狀態）。"""
    pend = [t for t in _load()["tasks"] if t["status"] == "pending"]
    pend.sort(key=lambda t: t["created_at"])
    return pend[0] if pend else None


def set_status(task_id: int, status: str, **fields) -> dict | None:
    """更新任務狀態與其他欄位（session_id 等）；in_progress 時 attempts +1。"""
    if status not in VALID_STATUS:
        raise ValueError(f"invalid status: {status}")
    with _locked():
        data = _load()
        for t in data["tasks"]:
            if t["id"] == task_id:
                t["status"] = status
                if status == "in_progress":
                    t["attempts"] = t.get("attempts", 0) + 1
                t["updated_at"] = time.time()
                t.update(fields)
                _save(data)
                return t
        return None


def counts() -> dict:
    c = {s: 0 for s in VALID_STATUS}
    for t in _load()["tasks"]:
        c[t["status"]] = c.get(t["status"], 0) + 1
    return c
