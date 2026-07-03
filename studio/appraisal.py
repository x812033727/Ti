"""考核庫 —— AI 成員績效的長期紀錄（績效感知派工的資料源）。

每場收尾檢討時 PM 對各參與 AI 打 1–5 分（`考核:` 行，flow.parse_appraisals 解析），
與流程既有客觀指標（QA 輪數／裁決、高工核可、耗時）合併成一筆筆考核紀錄，持久化到
單一 JSON 檔；拆解與 per-task 派工時以 summary() 聚合成 {provider: 平均分}，讓
flow.choose_dispatch 在同用量時偏好歷史表現好的 provider、PM 拆解 prompt 附「近期考核」摘要。

存法鏡射 lessons.py：單一 JSON 檔 + 檔案鎖序列化 read-modify-write，多 session 程序安全
增寫；檔案上限裁剪（保留最新 APPRAISAL_MAX_STORE 筆）封住長跑下只增不減。純檔案 IO、
與 LLM 解耦，方便單元測試（測試時 monkeypatch config.APPRAISALS_FILE 指向 tmp）。
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import time
from pathlib import Path

from . import config


def _path() -> Path:
    return config.APPRAISALS_FILE


def _lock_path() -> Path:
    return config.APPRAISALS_FILE.with_suffix(".lock")


@contextlib.contextmanager
def _locked():
    """以獨立 lock 檔序列化 read-modify-write，跨程序安全（鏡射 lessons._locked）。"""
    _path().parent.mkdir(parents=True, exist_ok=True)
    lock = _lock_path().open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


def _load() -> dict:
    """讀庫；檔案不存在／壞損（非 JSON、形狀不對）一律回空庫，絕不 raise。"""
    p = _path()
    if not p.is_file():
        return {"appraisals": []}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("appraisals"), list):
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return {"appraisals": []}


def _save(data: dict) -> None:
    _path().parent.mkdir(parents=True, exist_ok=True)
    tmp = _path().with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(_path())


def _clean(entry: dict) -> dict | None:
    """正規化一筆考核；不合格（score 非 1–5 整數、provider 與 role 皆空）回 None。

    欄位固定為 {session_id, task_id, role, provider, model, score, comment, objective,
    created_at}：provider 正規化小寫、comment 截長防塞爆、objective 僅收 dict（其餘視為
    無客觀指標）、created_at 呼叫端未給（或非數字）時取現在。防呆放存層——上游（LLM 輸出
    解析）再嚴，壞形狀也不得寫進長期庫。
    """
    if not isinstance(entry, dict):
        return None
    try:
        score = int(entry.get("score"))
    except (TypeError, ValueError):
        return None
    if not 1 <= score <= 5:
        return None
    provider = str(entry.get("provider") or "").strip().lower()
    role = str(entry.get("role") or "").strip()
    if not provider and not role:
        return None
    objective = entry.get("objective")
    try:
        created_at = float(entry.get("created_at"))
    except (TypeError, ValueError):
        created_at = time.time()
    task_id = entry.get("task_id")
    return {
        "session_id": str(entry.get("session_id") or ""),
        "task_id": task_id if isinstance(task_id, int) else None,
        "role": role,
        "provider": provider,
        "model": str(entry.get("model") or "").strip(),
        "score": score,
        "comment": str(entry.get("comment") or "").strip()[:200],
        "objective": objective if isinstance(objective, dict) else {},
        "created_at": created_at,
    }


def record(entries: list[dict]) -> None:
    """批次寫入考核紀錄（呼叫端已解析／合併好的 dict 列表）。

    壞筆（見 _clean）逐筆丟棄不擋整批；寫入後由新到舊裁剪至 APPRAISAL_MAX_STORE 筆。
    全批無效即 no-op（不動檔案）。
    """
    cleaned = [c for e in entries or [] if (c := _clean(e)) is not None]
    if not cleaned:
        return
    with _locked():
        data = _load()
        data["appraisals"].extend(cleaned)
        data["appraisals"] = data["appraisals"][-config.APPRAISAL_MAX_STORE :]
        _save(data)


def _stats(rows: list[dict]) -> dict:
    """一組考核列的聚合：平均分、樣本數、QA 通過率（無客觀裁決樣本時 None）。"""
    scores = [r["score"] for r in rows]
    judged = [
        v
        for r in rows
        if isinstance(r.get("objective"), dict)
        and (v := r["objective"].get("qa_passed")) is not None
    ]
    return {
        "avg_score": round(sum(scores) / len(scores), 2),
        "n": len(rows),
        "pass_rate": round(sum(1 for v in judged if v) / len(judged), 2) if judged else None,
    }


def summary(limit_days: int = 30) -> dict:
    """近 limit_days 天的考核聚合，回 ``{"providers": {...}, "models": {...}}`` 兩層。

    providers 層鍵為 provider 名、models 層鍵為 ``"<provider>/<model>"``（model 空者不入
    此層），值皆為 {avg_score, n, pass_rate}——plain dict、可直接 JSON 序列化，供派工
    performance（取 providers 層 avg_score）與 /api/appraisals 共用。limit_days <= 0 ＝
    不限天數。壞檔／空庫回兩層皆空。
    """
    cutoff = time.time() - limit_days * 86400 if limit_days > 0 else float("-inf")
    rows = [
        r
        for r in _load()["appraisals"]
        if isinstance(r, dict)
        and isinstance(r.get("score"), int)
        and r.get("provider")
        and (r.get("created_at") or 0) >= cutoff
    ]
    by_provider: dict[str, list[dict]] = {}
    by_model: dict[str, list[dict]] = {}
    for r in rows:
        by_provider.setdefault(r["provider"], []).append(r)
        if r.get("model"):
            by_model.setdefault(f"{r['provider']}/{r['model']}", []).append(r)
    return {
        "providers": {k: _stats(v) for k, v in by_provider.items()},
        "models": {k: _stats(v) for k, v in by_model.items()},
    }


def recent(limit: int = 50) -> list[dict]:
    """取最新 limit 筆考核（由新到舊，依儲存序）；limit <= 0 回空清單。供 API 檢視。"""
    if limit <= 0:
        return []
    return list(reversed(_load()["appraisals"]))[:limit]


def all_appraisals() -> list[dict]:
    """回傳全部考核（依儲存序，舊→新）；供檢視／測試。"""
    return list(_load()["appraisals"])
