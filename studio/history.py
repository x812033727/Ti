"""Session 歷史存檔 —— 把每次工作室執行的事件落地，供日後列表與重播。

每個 session 存成兩個檔：
  history/<id>.jsonl       逐行 JSON 的事件串流（依發生順序）
  history/<id>.meta.json   摘要（需求、時間、狀態、事件數）

純檔案 IO、與 LLM 解耦，方便單元測試。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from . import config


def _safe_id(session_id: str) -> str:
    safe = "".join(c for c in session_id if c.isalnum() or c in "-_")
    return safe or "default"


def _events_path(session_id: str) -> Path:
    return config.HISTORY_ROOT / f"{_safe_id(session_id)}.jsonl"


def _meta_path(session_id: str) -> Path:
    return config.HISTORY_ROOT / f"{_safe_id(session_id)}.meta.json"


def start_session(session_id: str, requirement: str) -> dict:
    """建立 session 的歷史檔與初始 meta（狀態 running）。"""
    config.HISTORY_ROOT.mkdir(parents=True, exist_ok=True)
    _events_path(session_id).write_text("", encoding="utf-8")
    meta = {
        "session_id": _safe_id(session_id),
        "requirement": requirement,
        "started_at": time.time(),
        "status": "running",   # running / completed / incomplete / stopped / error
        "n_events": 0,
    }
    _write_meta(session_id, meta)
    return meta


def record_event(session_id: str, event: dict) -> None:
    """附加一則事件到 jsonl（若 session 未建立則略過）。"""
    path = _events_path(session_id)
    if not path.parent.exists():
        return
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


def finish_session(session_id: str) -> dict | None:
    """讀回事件、推導最終狀態並更新 meta。"""
    meta = get_meta(session_id)
    if meta is None:
        return None
    events = load_events(session_id)
    meta["n_events"] = len(events)
    meta["finished_at"] = time.time()
    meta["status"] = _derive_status(events)
    _write_meta(session_id, meta)
    return meta


def _derive_status(events: list[dict]) -> str:
    for ev in reversed(events):
        if ev.get("type") == "error":
            return "error"
    for ev in reversed(events):
        if ev.get("type") == "done":
            p = ev.get("payload", {})
            if p.get("stopped"):
                return "stopped"
            return "completed" if p.get("completed") else "incomplete"
    return "incomplete"


def list_sessions() -> list[dict]:
    """回傳所有 session 的 meta，依開始時間新到舊。"""
    root = config.HISTORY_ROOT
    if not root.exists():
        return []
    metas: list[dict] = []
    for p in root.glob("*.meta.json"):
        try:
            metas.append(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    metas.sort(key=lambda m: m.get("started_at", 0), reverse=True)
    return metas


def get_meta(session_id: str) -> dict | None:
    path = _meta_path(session_id)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def load_events(session_id: str) -> list[dict]:
    path = _events_path(session_id)
    if not path.is_file():
        return []
    events: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def _write_meta(session_id: str, meta: dict) -> None:
    _meta_path(session_id).write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
