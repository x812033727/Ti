"""週報 digest(功能強化 D2):把一段時間窗的成果彙整成純模板 markdown——零 LLM、即時生成。

口徑與 insights(D1 趨勢)共用 OK/FAIL 分類常數(單一真相,兩邊數字必須一致);完成率
delta 以「前一個等長窗」對照(前窗超出 audit 保留期時 prev=None,不顯示 delta)。
"""

from __future__ import annotations

import re
import time
from pathlib import Path

from . import backlog, config, insights, lessons, secure_write
from .release_note import pyproject_version


def _window_stats(records: list[dict], start: float, end: float) -> dict:
    counts: dict[str, int] = {}
    ok = fail = 0
    for rec in records:
        try:
            ts = float(rec.get("ts", 0))
        except (TypeError, ValueError):
            continue
        if not (start <= ts < end):
            continue
        outcome = str(rec.get("outcome") or "unknown")
        counts[outcome] = counts.get(outcome, 0) + 1
        if outcome in insights.OK_OUTCOMES:
            ok += 1
        elif outcome in insights.FAIL_OUTCOMES:
            fail += 1
    total = ok + fail
    return {
        "counts": counts,
        "ok": ok,
        "fail": fail,
        "rate": round(ok / total, 3) if total else None,
    }


def _utc_date(ts: float) -> str:
    t = time.gmtime(ts)
    return f"{t.tm_year:04d}-{t.tm_mon:02d}-{t.tm_mday:02d}"


def build_digest(days: int = 7) -> dict:
    """彙整近 days 天(UTC)的成果。days 夾 1..30。"""
    days = max(1, min(days, 30))
    now = time.time()
    start = now - days * 86400
    records = insights._read_audit()
    cur = _window_stats(records, start, now)
    prev = _window_stats(records, start - days * 86400, start)
    # 前窗完全無紀錄(可能超出 audit 保留期)→ delta 不顯示
    delta = (
        round(cur["rate"] - prev["rate"], 3)
        if cur["rate"] is not None and prev["rate"] is not None
        else None
    )
    prs = [
        {
            "pr": rec.get("pr"),
            "task_id": rec.get("task_id"),
            "ts": rec.get("ts"),
        }
        for rec in records
        if rec.get("outcome") == "merged"
        and rec.get("pr")
        and start <= float(rec.get("ts", 0) or 0) < now
    ]
    titles = {t["id"]: t.get("title", "") for t in backlog.list_tasks()}
    for p in prs:
        p["title"] = titles.get(p.get("task_id"), "")
    return {
        "window": {"from": _utc_date(start), "to": _utc_date(now), "days": days},
        "counts": cur["counts"],
        "completion_rate": cur["rate"],
        "prev_completion_rate": prev["rate"],
        "delta": delta,
        "prs": prs,
        "lessons_top": [it.get("text", "") for it in lessons.recent(5)],
        "north_star": config.AUTOPILOT_NORTH_STAR,
        "backlog_counts": backlog.counts(),
        "version": pyproject_version(),
    }


def render_markdown(digest: dict) -> str:
    """把 digest 渲染成人讀 markdown(純 f-string 模板,零 LLM)。"""
    w = digest["window"]
    rate = digest["completion_rate"]
    prev = digest["prev_completion_rate"]
    delta = digest["delta"]
    rate_s = f"{round(rate * 100)}%" if rate is not None else "—"
    prev_s = f"(前週 {round(prev * 100)}%" if prev is not None else ""
    if prev_s:
        prev_s += (
            f"、{'+' if (delta or 0) >= 0 else ''}{round((delta or 0) * 100)}pp)"
            if delta is not None
            else ")"
        )
    counts_s = "・".join(f"{k} {v}" for k, v in sorted(digest["counts"].items())) or "無終局紀錄"
    bc = digest["backlog_counts"]
    lines = [
        f"## Ti 週報 {w['from']} ~ {w['to']}(v{digest['version']})",
        "",
        f"**北極星**:{digest['north_star']}",
        "",
        f"**完成率**:{rate_s} {prev_s}".rstrip(),
        f"**終局分佈**:{counts_s}",
        f"**backlog**:pending {bc.get('pending', 0)}・merging {bc.get('merging', 0)}・done {bc.get('done', 0)}・failed {bc.get('failed', 0)}・parked {bc.get('parked', 0)}",
        "",
        f"### 本窗合併 PR({len(digest['prs'])})",
    ]
    for p in digest["prs"]:
        lines.append(f"- #{p['pr']} {p.get('title') or ''}(任務 #{p.get('task_id')})")
    if not digest["prs"]:
        lines.append("- (無)")
    lines.append("")
    lines.append("### 近期教訓 Top 5")
    for t in digest["lessons_top"]:
        lines.append(f"- {t}")
    if not digest["lessons_top"]:
        lines.append("- (無)")
    return "\n".join(lines)


# --- 落盤與歷史(第五輪 F6):digest 不再「關掉面板即失」------------------------

# 檔名固定 digest-YYYY-MM-DD.md(UTC 日):正則同時是 read_digest 的路徑穿越防線
# (只有命中的檔名才會被讀,../ 之類一律 None)。
_NAME_RE = re.compile(r"^digest-\d{4}-\d{2}-\d{2}\.md$")


def _digests_dir() -> Path:
    return config.AUTOPILOT_STATE_DIR / "digests"


def save_digest(days: int = 7, now: float | None = None) -> str:
    """產出並落盤當日(UTC)的 digest 檔,回傳檔名;同日重呼叫覆寫(冪等)。"""
    ts = time.time() if now is None else now
    name = f"digest-{_utc_date(ts)}.md"
    d = _digests_dir()
    d.mkdir(parents=True, exist_ok=True)
    secure_write.secure_write_root(d / name, render_markdown(build_digest(days)).encode("utf-8"))
    return name


def list_digests() -> list[dict]:
    """已落盤 digest 清單(新→舊):[{name, mtime}]。目錄不存在=空清單。"""
    d = _digests_dir()
    out = []
    try:
        for p in d.iterdir():
            if _NAME_RE.match(p.name):
                out.append({"name": p.name, "mtime": p.stat().st_mtime})
    except OSError:
        return []
    out.sort(key=lambda x: x["name"], reverse=True)
    return out


def read_digest(name: str) -> str | None:
    """讀單一 digest 內容;檔名不合法(路徑穿越)或不存在回 None。"""
    if not _NAME_RE.match(name or ""):
        return None
    try:
        return (_digests_dir() / name).read_text(encoding="utf-8")
    except OSError:
        return None
