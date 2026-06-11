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

VALID_STATUS = ("pending", "in_progress", "done", "failed")


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
    _dir(state_dir).mkdir(parents=True, exist_ok=True)
    tmp = _path(state_dir).with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(_path(state_dir))


def add(
    title: str, detail: str = "", source: str = "seed", *, state_dir: Path | None = None
) -> dict | None:
    """新增一筆 pending 任務，回傳該任務；title 為空或重複則回 None。"""
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
    """取最早建立、仍 pending 的任務（不改狀態）。"""
    pend = [t for t in _load(state_dir)["tasks"] if t["status"] == "pending"]
    pend.sort(key=lambda t: t["created_at"])
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
