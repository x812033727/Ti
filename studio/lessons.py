"""跨場次教訓庫 —— 工作室的長期記憶。

每場討論的檢討會蒸餾出可重用的「教訓」（踩過的坑、有效做法、技術選型結論），
持久化到單一 JSON 檔；下次新討論開場時注入 PM 拆解，讓工作室跨場次自我加強——
避免重蹈覆轍、善用既有結論。

存法與 backlog 一致：單一 JSON 檔 + 檔案鎖序列化 read-modify-write，讓多個 session
程序安全增寫。純檔案 IO、與 LLM 解耦，方便單元測試（測試時用 TI_LESSONS_FILE 指向 tmp）。
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import time
from pathlib import Path

from . import config

# 檔案最多保留幾筆（由新到舊截斷），封住長跑下只增不減。注入時另以 LESSONS_MAX 取最新 N 筆。
_MAX_STORE = 500


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


def _load() -> dict:
    p = _path()
    if not p.is_file():
        return {"lessons": []}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("lessons"), list):
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return {"lessons": []}


def _save(data: dict) -> None:
    _path().parent.mkdir(parents=True, exist_ok=True)
    tmp = _path().with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(_path())


def add_many(texts: list[str], *, session_id: str = "", requirement: str = "") -> int:
    """批次新增教訓（對既有內容去重），回傳實際新增數。

    去重採「全文（去前後空白）完全相符」：同一句教訓只留一筆，避免每場重提把庫塞爆。
    """
    cleaned = [t.strip() for t in texts if t and t.strip()]
    if not cleaned:
        return 0
    with _locked():
        data = _load()
        existing = {item["text"].strip() for item in data["lessons"]}
        n = 0
        for text in cleaned:
            if text in existing:
                continue
            data["lessons"].append(
                {
                    "text": text,
                    "session_id": session_id,
                    "requirement": (requirement or "")[:200],
                    "created_at": time.time(),
                }
            )
            existing.add(text)
            n += 1
        if n:
            # 只保留最新 _MAX_STORE 筆（依出現序，新的在尾端）。
            data["lessons"] = data["lessons"][-_MAX_STORE:]
            _save(data)
        return n


def recent(limit: int) -> list[dict]:
    """取最新 limit 筆教訓（由新到舊）。limit <= 0 回空清單。"""
    if limit <= 0:
        return []
    return list(reversed(_load()["lessons"]))[:limit]


def context(limit: int | None = None) -> str:
    """組成要注入 PM 拆解 prompt 的教訓區塊；停用、無教訓或 limit<=0 時回 ""。"""
    if not config.LESSONS_ENABLED:
        return ""
    cap = config.LESSONS_MAX if limit is None else limit
    rows = recent(cap)
    if not rows:
        return ""
    body = "\n".join(f"- {r['text']}" for r in rows)
    return f"【跨場次教訓庫（過往各場討論檢討蒸餾，請避免重蹈覆轍、善用既有結論）】\n{body}\n\n"


def all_lessons() -> list[dict]:
    """回傳全部教訓（依儲存序，舊→新）；供檢視 / 測試。"""
    return list(_load()["lessons"])
