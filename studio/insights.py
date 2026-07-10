"""看板洞察聚合（功能強化 D1）：audit 趨勢 / 調查結論清單——純檔案 IO、無 LLM。

audit.jsonl 是 autopilot 每筆終局的結構化審計（ts/task_id/pr/outcome/detail/duration_s/
attempts，append-only，保留約 30 天後壓實搬 .old——趨勢以現役檔為準）。原本只被後端當
每日 PR 計數，本模組把它聚成看板可用的每日 outcome 分佈與完成率；OK/FAIL 分類常數是
D1 趨勢與 D2 週報的**單一口徑**（兩邊數字必須一致）。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from . import backlog, config

# 完成率口徑：終局成敗桶。merge_pending/no_changes/investigation_parked/escalated 等
# 「非成敗終局」與未來新增的未知 outcome 進 outcomes 明細但不進分母（前向相容）。
OK_OUTCOMES = frozenset({"merged", "investigation_done"})
FAIL_OUTCOMES = frozenset({"merge_failed", "investigation_refuted"})


def _audit_path(state_dir: Path | None = None) -> Path:
    return (state_dir or config.AUTOPILOT_STATE_DIR) / "audit.jsonl"


def _read_audit(state_dir: Path | None = None) -> list[dict]:
    """逐行 parse audit.jsonl；壞行/壞 ts 跳過（與 _todays_pr_count 同容錯）。"""
    path = _audit_path(state_dir)
    if not path.is_file():
        return []
    out: list[dict] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
            if isinstance(rec, dict):
                out.append(rec)
        except json.JSONDecodeError:
            continue
    return out


def _utc_day(ts: float) -> str:
    t = time.gmtime(ts)
    return f"{t.tm_year:04d}-{t.tm_mon:02d}-{t.tm_mday:02d}"


def audit_trend(days: int = 30, *, state_dir: Path | None = None) -> dict:
    """近 days 天的每日 outcome 分佈與完成率（UTC 日,與每日 PR 熔斷同口徑）。

    回傳 {"days", "buckets": [{date, outcomes, ok, fail, rate}...只含有紀錄的日、由舊到新],
    "totals": {ok, fail, rate}}；rate 無終局紀錄時 None。days 夾 1..90。
    """
    days = max(1, min(days, 90))
    cutoff = time.time() - days * 86400
    buckets: dict[str, dict] = {}
    for rec in _read_audit(state_dir):
        try:
            ts = float(rec.get("ts", 0))
        except (TypeError, ValueError):
            continue
        if ts < cutoff:
            continue
        day = _utc_day(ts)
        b = buckets.setdefault(day, {"date": day, "outcomes": {}, "ok": 0, "fail": 0})
        outcome = str(rec.get("outcome") or "unknown")
        b["outcomes"][outcome] = b["outcomes"].get(outcome, 0) + 1
        if outcome in OK_OUTCOMES:
            b["ok"] += 1
        elif outcome in FAIL_OUTCOMES:
            b["fail"] += 1
    ordered = [buckets[d] for d in sorted(buckets)]
    for b in ordered:
        terminal = b["ok"] + b["fail"]
        b["rate"] = round(b["ok"] / terminal, 3) if terminal else None
    ok = sum(b["ok"] for b in ordered)
    fail = sum(b["fail"] for b in ordered)
    total = ok + fail
    return {
        "days": days,
        "buckets": ordered,
        "totals": {"ok": ok, "fail": fail, "rate": round(ok / total, 3) if total else None},
    }


# 調查任務的 note 前綴（autopilot 調查分流管線落檔慣例）。
_INVESTIGATION_NOTE_PREFIXES = ("[調查結論]", "[調查]")


def investigations(limit: int = 50, *, state_dir: Path | None = None) -> list[dict]:
    """調查任務清單（note 帶 [調查結論]/[調查] 前綴），join audit 的 investigation_* 紀錄。

    調查結論原本只散在 backlog note 與教訓庫,看板無從閱讀——這是唯一的彙整視圖。
    由新到舊,limit 夾 1..500。
    """
    limit = max(1, min(limit, 500))
    audit_by_task: dict[str, dict] = {}
    for rec in _read_audit(state_dir):
        if str(rec.get("outcome") or "").startswith("investigation"):
            audit_by_task[str(rec.get("task_id"))] = rec  # 後者覆蓋=取最新
    out: list[dict] = []
    for t in backlog.list_tasks(state_dir=state_dir):
        note = str(t.get("note") or "")
        if not note.startswith(_INVESTIGATION_NOTE_PREFIXES):
            continue
        item = {
            "task_id": t["id"],
            "title": t.get("title", ""),
            "status": t.get("status", ""),
            "note": note,
            "updated_at": t.get("updated_at", 0),
        }
        rec = audit_by_task.get(str(t["id"]))
        if rec:
            item["outcome"] = rec.get("outcome")
            item["duration_s"] = rec.get("duration_s")
            item["attempts"] = rec.get("attempts")
        out.append(item)
    out.sort(key=lambda x: x.get("updated_at", 0), reverse=True)
    return out[:limit]
