"""排程任務(Kimi 化 PR10):使用者定義的週期性 autopilot 任務。

Kimi 的「Scheduled Tasks」在 Ti 的對應:排程只是「到期把任務插進 backlog」——
執行引擎仍是 autopilot 既有佇列,排程器不新增任何執行路徑。

recurrence 刻意簡化為三種(cron 語法=支援面過大):
  {"kind": "daily",          "time": "HH:MM"}                 每日 UTC 該時刻後首個 tick
  {"kind": "weekly",         "time": "HH:MM", "weekday": 0-6} 每週該日(0=週一)該時刻後
  {"kind": "interval_hours", "hours": 1-168}                  每 N 小時一次(UTC epoch 對齊桶)

去重(仿 digest「當日一次」模式):每個排程對每個 occurrence 產生唯一 key
(daily=UTC 日期、weekly=UTC 日期、interval=epoch 桶號),key 落盤在 last_fired_key,
tick 重跑/行程重啟不重複入列;backlog.add 的同標題查重是第二道防線(前一次還沒消化
完=跳過本次,不堆積)。

併發:web(CRUD)與 autopilot(enqueue_due 回寫 last_fired_key)雙行程寫同一檔,
一律走 flock(仿 backlog._locked 範式)。
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import time
import uuid
from pathlib import Path

from . import backlog, config
from .secure_write import secure_write_root

log = logging.getLogger("ti.schedules")

KINDS = ("daily", "weekly", "interval_hours")
_TITLE_MAX = 200
_DETAIL_MAX = 4000


def _dir(state_dir: Path | None) -> Path:
    return state_dir or config.AUTOPILOT_STATE_DIR


def _path(state_dir: Path | None = None) -> Path:
    return _dir(state_dir) / "schedules.json"


@contextlib.contextmanager
def _locked(state_dir: Path | None = None):
    _dir(state_dir).mkdir(parents=True, exist_ok=True)
    lock = (_dir(state_dir) / "schedules.lock").open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


def _load(state_dir: Path | None = None) -> dict:
    p = _path(state_dir)
    if not p.is_file():
        return {"schedules": []}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("schedules"), list):
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return {"schedules": []}


def _save(data: dict, state_dir: Path | None = None) -> None:
    _dir(state_dir).mkdir(parents=True, exist_ok=True)
    secure_write_root(
        _path(state_dir), json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    )


def validate_recurrence(rec: dict) -> str:
    """回錯誤訊息;合法回空字串。"""
    if not isinstance(rec, dict) or rec.get("kind") not in KINDS:
        return f"recurrence.kind 須為 {'/'.join(KINDS)}"
    kind = rec["kind"]
    if kind in ("daily", "weekly"):
        t = str(rec.get("time") or "")
        parts = t.split(":")
        try:
            ok = len(parts) == 2 and 0 <= int(parts[0]) <= 23 and 0 <= int(parts[1]) <= 59
        except ValueError:
            ok = False
        if not ok:
            return "time 須為 HH:MM(UTC)"
    if kind == "weekly":
        try:
            if not 0 <= int(rec.get("weekday", -1)) <= 6:
                return "weekday 須為 0-6(0=週一)"
        except (TypeError, ValueError):
            return "weekday 須為 0-6(0=週一)"
    if kind == "interval_hours":
        try:
            if not 1 <= int(rec.get("hours", 0)) <= 168:
                return "hours 須為 1-168"
        except (TypeError, ValueError):
            return "hours 須為 1-168"
    return ""


def occurrence_key(sched: dict, now: float) -> str | None:
    """now 這一刻該排程「應已觸發」的 occurrence key;尚未到期回 None。"""
    rec = sched.get("recurrence") or {}
    kind = rec.get("kind")
    t = time.gmtime(now)
    if kind in ("daily", "weekly"):
        hh, mm = (int(x) for x in str(rec.get("time", "00:00")).split(":"))
        if (t.tm_hour, t.tm_min) < (hh, mm):
            return None
        if kind == "weekly" and t.tm_wday != int(rec.get("weekday", 0)):
            return None
        return f"{kind[0]}-{t.tm_year:04d}{t.tm_mon:02d}{t.tm_mday:02d}"
    if kind == "interval_hours":
        bucket = int(now // (int(rec.get("hours", 1)) * 3600))
        return f"i-{bucket}"
    return None


# --- CRUD(web 行程) --------------------------------------------------------


def list_schedules(*, state_dir: Path | None = None) -> list[dict]:
    return _load(state_dir)["schedules"]


def create(
    title: str,
    detail: str,
    recurrence: dict,
    *,
    priority: int = 1,
    item_type: str = "improvement",
    state_dir: Path | None = None,
) -> tuple[dict | None, str]:
    """回 (schedule, "") 或 (None, 錯誤訊息)。"""
    title = (title or "").strip()[:_TITLE_MAX]
    if not title:
        return None, "標題不可為空"
    err = validate_recurrence(recurrence)
    if err:
        return None, err
    sched = {
        "id": uuid.uuid4().hex[:12],
        "title": title,
        "detail": (detail or "").strip()[:_DETAIL_MAX],
        "priority": max(0, min(2, int(1 if priority is None else priority))),  # 0 是合法值,勿用 or
        "type": item_type if item_type in ("feature", "bug", "improvement") else "improvement",
        "recurrence": recurrence,
        "enabled": True,
        "created_at": time.time(),
        "last_fired_key": "",
    }
    with _locked(state_dir):
        data = _load(state_dir)
        data["schedules"].append(sched)
        _save(data, state_dir)
    return sched, ""


def update(
    sched_id: str, fields: dict, *, state_dir: Path | None = None
) -> tuple[dict | None, str]:
    """可改 title/detail/priority/type/recurrence/enabled;回 (schedule, "") 或 (None, 錯)。"""
    if "recurrence" in fields:
        err = validate_recurrence(fields["recurrence"])
        if err:
            return None, err
    with _locked(state_dir):
        data = _load(state_dir)
        for s in data["schedules"]:
            if s["id"] != sched_id:
                continue
            if "title" in fields:
                title = str(fields["title"] or "").strip()[:_TITLE_MAX]
                if not title:
                    return None, "標題不可為空"
                s["title"] = title
            if "detail" in fields:
                s["detail"] = str(fields["detail"] or "").strip()[:_DETAIL_MAX]
            if "priority" in fields:
                s["priority"] = max(
                    0, min(2, int(1 if fields["priority"] is None else fields["priority"]))
                )
            if "type" in fields and fields["type"] in ("feature", "bug", "improvement"):
                s["type"] = fields["type"]
            if "recurrence" in fields:
                s["recurrence"] = fields["recurrence"]
            if "enabled" in fields:
                s["enabled"] = bool(fields["enabled"])
            _save(data, state_dir)
            return s, ""
    return None, "不存在的排程"


def delete(sched_id: str, *, state_dir: Path | None = None) -> bool:
    with _locked(state_dir):
        data = _load(state_dir)
        before = len(data["schedules"])
        data["schedules"] = [s for s in data["schedules"] if s["id"] != sched_id]
        if len(data["schedules"]) == before:
            return False
        _save(data, state_dir)
        return True


# --- 到期入列(autopilot 行程,主迴圈 tick 呼叫) ----------------------------


def enqueue_due(now: float | None = None, *, state_dir: Path | None = None) -> int:
    """把到期且本 occurrence 未觸發過的排程插入 backlog;回入列數。

    任何單一排程的錯誤只 log 不擴散;backlog.add 回 None(同標題已存在=前次未消化)
    視為本次跳過,但 key 照記——「跳過」也是這個 occurrence 的處置,下個 occurrence 再來。
    """
    t = now if now is not None else time.time()
    fired = 0
    with _locked(state_dir):
        data = _load(state_dir)
        dirty = False
        for s in data["schedules"]:
            try:
                if not s.get("enabled") or not str(s.get("title") or "").strip():
                    continue  # 停用或壞資料(無標題)一律跳過
                key = occurrence_key(s, t)
                if not key or key == s.get("last_fired_key"):
                    continue
                task = backlog.add(
                    f"[排程] {s['title']}",
                    s.get("detail", ""),
                    source="schedule",
                    priority=int(s.get("priority", 1)),
                    item_type=s.get("type", "improvement"),
                )
                s["last_fired_key"] = key
                dirty = True
                if task is not None:
                    fired += 1
                    log.info("排程 %s 到期入列:任務 #%s(%s)", s["id"], task["id"], key)
                else:
                    log.info("排程 %s 到期但同標題任務尚未消化,本次跳過(%s)", s["id"], key)
            except Exception:  # noqa: BLE001 — 單一排程壞資料不得擴散
                log.warning("排程 %s 入列失敗(忽略)", s.get("id"), exc_info=True)
        if dirty:
            _save(data, state_dir)
    return fired
