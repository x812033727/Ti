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


# --- 「需要你」例外收件匣(第 4 階按例外監控,軌 F1) ---------------------------
# 收件匣事件=page 級(需要人行動)但排除自證/日報型雜訊;用 severity 動態判定,
# 未來新增的 page kind 自動進收件匣(fail-loud,與 notify 同哲學)。
_ATTENTION_EVENT_EXCLUDE = frozenset({"test", "daily_digest"})
_ATTENTION_TASK_FIELDS = ("id", "title", "note", "clarify", "updated_at", "source", "attempts")


def attention(days: int = 7, *, state_dir: Path | None = None) -> dict:
    """例外收件匣聚合:澄清待答票/停放任務+原因/近 days 天 page 級事件。

    純檔案讀取零 LLM。澄清票=parked 且 clarify 非空(答覆走既有 task action
    unpark+note);badge 數=待答澄清票數。days 夾 1..30。
    """
    days = max(1, min(30, int(days)))
    clarify: list[dict] = []
    parked: list[dict] = []
    for t in backlog.list_tasks("parked", state_dir=state_dir):
        row = {k: t.get(k) for k in _ATTENTION_TASK_FIELDS}
        (clarify if str(t.get("clarify") or "").strip() else parked).append(row)
    clarify.sort(key=lambda r: r.get("updated_at") or 0, reverse=True)
    parked.sort(key=lambda r: r.get("updated_at") or 0, reverse=True)
    events = [
        {k: e.get(k) for k in ("kind", "title", "task_id", "ts") if e.get(k) is not None}
        for e in notify.read_events(days, state_dir=state_dir)
        if notify.severity(str(e.get("kind") or "")) == "page"
        and e.get("kind") not in _ATTENTION_EVENT_EXCLUDE
    ]
    events.sort(key=lambda e: e.get("ts") or 0, reverse=True)
    return {
        "clarify": clarify[:50],
        "parked": parked[:50],
        "events": events[:50],
        "pending_clarify": len(clarify),
    }


# --- 升階儀表(第 3/4 階可視化,軌 D1) ----------------------------------------
# 八個 canary 開關的單一真相表:(鍵, 人話標籤, config 取值函數)。順序=建議開啟順序。
_CANARIES = (
    ("objective_gate", "① 客觀驗收閘門", lambda c: bool(c.objective_gate_enabled())),
    ("expert_skills", "② 專家技能手冊", lambda c: bool(c.EXPERT_SKILLS)),
    (
        "investigation_parallel",
        "③ 調查併行旁路",
        lambda c: bool(c.AUTOPILOT_INVESTIGATION_PARALLEL),
    ),
    ("norms_loop", "④ 規範蒸餾迴路", lambda c: bool(c.NORMS_LOOP)),
    ("slo_brake", "⑤ SLO 自動煞車", lambda c: float(getattr(c, "SLO_ZERO_TOUCH_MIN", 0) or 0) > 0),
    ("deploy_verify", "⑥ 部署黑盒驗證", lambda c: bool(c.DEPLOY_VERIFY)),
    ("clarify_async", "⑦ 非同步澄清", lambda c: bool(c.CLARIFY_ASYNC)),
    ("intent_loop", "⑧ 意圖迴路", lambda c: bool(c.INTENT_LOOP)),
)


def stage_readiness(*, state_dir: Path | None = None) -> dict:
    """升階儀表快照:八開關現值+第 3 階四條件量測+階段判定。

    純快照(連續天數 streak 留後續);「紅色事件全由推播抵達」真值不可自動量測,
    以代理呈現(page 級事件計數+推播 sinks 是否已設)。零 LLM、純檔案/config 讀取。
    """
    canaries = [{"key": k, "label": label, "on": bool(fn(config))} for k, label, fn in _CANARIES]
    on_count = sum(1 for c in canaries if c["on"])
    m = trust_metrics(7, state_dir=state_dir)
    iv = m.get("interventions", {})
    by_cat = iv.get("by_category", {})
    ev = m.get("events", {})
    page_kinds = (
        "task_failed",
        "loop_stall",
        "quota_exhausted",
        "slo_brake",
        "deploy_verify_failed",
        "clarify_pending",
    )
    sinks_ready = bool(
        (config.NOTIFY_WEBHOOK or "").strip()
        or ((config.TELEGRAM_BOT_TOKEN or "").strip() and (config.TELEGRAM_CHAT_ID or "").strip())
    )
    conditions = [
        {
            "key": "zero_touch",
            "label": "零人工介入合併率 ≥90%",
            "value": m.get("zero_touch_rate"),
            "detail": f"7 天 merged {m.get('merged', 0)}、零介入 {m.get('zero_touch', 0)}",
            "ok": bool(m.get("merged", 0) >= 5 and (m.get("zero_touch_rate") or 0) >= 0.9),
        },
        {
            "key": "interventions",
            "label": "人工介入 ≤2/週且零成果審查",
            "value": iv.get("per_week"),
            "detail": f"成果審查 {by_cat.get('output_review', 0)}・補背景 {by_cat.get('context_feeding', 0)}・維運 {by_cat.get('ops', 0)}",
            "ok": bool((iv.get("per_week") or 0) <= 2 and by_cat.get("output_review", 0) == 0),
        },
        {
            "key": "paging",
            "label": "異常推播管道就緒(代理量測)",
            "value": sum(int(ev.get(k) or 0) for k in page_kinds),
            "detail": ("推播 sinks 已設" if sinks_ready else "推播 sinks 未設")
            + f"・7 天 page 級事件 {sum(int(ev.get(k) or 0) for k in page_kinds)} 筆",
            "ok": sinks_ready,
        },
        {
            "key": "slo_armed",
            "label": "SLO 煞車武裝",
            "value": int(ev.get("slo_brake") or 0),
            "detail": f"門檻 {'已設' if float(getattr(config, 'SLO_ZERO_TOUCH_MIN', 0) or 0) > 0 else '未設(=0)'}・觸發 {int(ev.get('slo_brake') or 0)} 次",
            "ok": float(getattr(config, "SLO_ZERO_TOUCH_MIN", 0) or 0) > 0,
        },
    ]
    all_ok = all(c["ok"] for c in conditions)
    streak = stage_streak(state_dir=state_dir)
    if on_count == 0:
        stage = "2"
    elif all_ok and streak >= 14 and on_count >= len(canaries) - 3:
        stage = "3-ready"
    else:
        stage = "3-progress"
    return {
        "stage": stage,
        "canaries": canaries,
        "canaries_on": on_count,
        "conditions": conditions,
        "streak": streak,
        "streak_target": 14,
        "trust": m,
    }


def _stage_history_path(state_dir: Path | None = None) -> Path:
    return (state_dir or config.AUTOPILOT_STATE_DIR) / "stage_history.jsonl"


def record_stage_snapshot(now: float | None = None, *, state_dir: Path | None = None) -> bool:
    """把當日(UTC)宣告條件快照落檔 stage_history.jsonl(一天一筆冪等);回是否新寫。

    「連續 14 天條件全綠」需要歷史——快照由 digest scheduler 每日呼叫(同一節拍)。
    """
    from . import jsonl_log

    t = now if now is not None else time.time()
    day = _utc_day(t)
    path = _stage_history_path(state_dir)
    for rec in jsonl_log.read_window(path, 3):
        if rec.get("day") == day:
            return False  # 當日已記,冪等
    snap = stage_readiness(state_dir=state_dir)
    jsonl_log.append(
        path,
        {
            "ts": t,
            "day": day,
            "all_ok": all(c["ok"] for c in snap["conditions"]),
            "conditions": {c["key"]: c["ok"] for c in snap["conditions"]},
            "canaries_on": snap["canaries_on"],
        },
    )
    return True


def stage_streak(*, state_dir: Path | None = None) -> int:
    """由新往舊數「四條件全綠」的連續天數(含今日快照若存在);斷檔=中斷。"""
    from . import jsonl_log

    recs = jsonl_log.read_window(_stage_history_path(state_dir), 60)
    by_day = {r.get("day"): bool(r.get("all_ok")) for r in recs if r.get("day")}
    if not by_day:
        return 0
    streak = 0
    t = time.time()
    for i in range(60):
        day = _utc_day(t - i * 86400)
        ok = by_day.get(day)
        if ok is None:
            if i == 0:
                continue  # 今日尚未快照,不中斷,從昨日起算
            break
        if not ok:
            break
        streak += 1
    return streak
