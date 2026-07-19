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

from . import backlog, config, interventions, notify

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


# 信任指標關注的系統事件 kind(events.jsonl):質量回饋(critic/gate)+異常(quota/stall/
# task_failed)。未列 kind 照樣進 events 明細——前向相容,新事件不用改這裡。
_TRUST_EVENT_KINDS = (
    "critic_reject",
    "gate_failure",
    "quota_exhausted",
    "loop_stall",
    "task_failed",
)


def trust_metrics(days: int = 7, *, state_dir: Path | None = None) -> dict:
    """第 3 階(監督式自治)信任指標:零人工介入合併率+介入分類+系統事件計數。

    口徑:
    - merged=視窗內 outcome=="merged" 的 audit 紀錄(直接合併與 reconciler 收斂各記
      一筆、不同任務不重複——同任務先記 merge_pending 非 merged,無雙計)。
    - zero_touch=merged 中,該 task_id 在視窗內無 output_review 類人工介入者。
      已知限制:繞過面板直接在 GitHub 上的人工操作不可見——口徑是「面板留痕的介入」。
    - first_try=attempts==0 的 merged;reconciled=由 reconciler 背景收斂的 merged。
    - events 只彙整計數,明細留在 events.jsonl。days 夾 1..90。
    """
    days = max(1, min(days, 90))
    cutoff = time.time() - days * 86400
    merged: list[dict] = []
    for rec in _read_audit(state_dir):
        try:
            ts = float(rec.get("ts", 0))
        except (TypeError, ValueError):
            continue
        if ts >= cutoff and str(rec.get("outcome") or "") == "merged":
            merged.append(rec)
    ints = interventions.read_window(days, state_dir=state_dir)
    reviewed_tasks = {
        str(i.get("task_id"))
        for i in ints
        if i.get("category") == "output_review" and i.get("task_id") is not None
    }
    zero = [r for r in merged if str(r.get("task_id")) not in reviewed_tasks]
    by_cat: dict[str, int] = {}
    for i in ints:
        cat = str(i.get("category") or "output_review")
        by_cat[cat] = by_cat.get(cat, 0) + 1
    events: dict[str, int] = dict.fromkeys(_TRUST_EVENT_KINDS, 0)
    for e in notify.read_events(days, state_dir=state_dir):
        kind = str(e.get("kind") or "unknown")
        events[kind] = events.get(kind, 0) + 1
    return {
        "days": days,
        "merged": len(merged),
        "zero_touch": len(zero),
        "zero_touch_rate": round(len(zero) / len(merged), 3) if merged else None,
        "first_try_merged": sum(1 for r in merged if not r.get("attempts")),
        "reconciled_merges": sum(1 for r in merged if r.get("reconciled")),
        "interventions": {
            "total": len(ints),
            "by_category": by_cat,
            "per_week": round(len(ints) / days * 7, 1),
        },
        "events": events,
    }
