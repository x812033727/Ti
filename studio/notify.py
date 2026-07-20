"""主動通知 webhook（功能第五輪 F2）＋系統事件留痕（第 3 階信任指標 A0）。

全系統原本零主動通知——任務失敗/額度耗盡/主迴圈停滯都要開面板輪詢才知道。
設 TI_NOTIFY_WEBHOOK（空=關，預設）後，關鍵異常會 POST 一筆 JSON：
    {"source": "ti", "kind": "<事件類型>", "title": "<一句人話>", ...extra}
端點自理路由（Slack/Discord/自架皆可作 relay）。

Telegram sink（第 3 階 A1）：TI_TELEGRAM_BOT_TOKEN + TI_TELEGRAM_CHAT_ID 皆非空即啟用，
與 webhook 並存、各自獨立成敗——「按例外監控」的前提是推播真的到手機。端到端驗證走
POST /api/notify/test（routes.py）。

A0 起，每則事件（無論 webhook 是否設定）都先落檔 autopilot/events.jsonl
（jsonl_log 範式）——quota_exhausted/loop_stall/task_failed 過去只有推播、不留痕，
信任指標（insights.trust_metrics）需要無條件的結構化計數。
`record()` 供「僅留痕不推播」的內部質量事件（critic_reject/gate_failure）使用：
這類事件是常態回饋訊號，推播出去只會是噪音。

設計約束：
- 零依賴（urllib）；失敗只 debug log 絕不冒泡——通知是加值，不得影響主迴圈。
- `send_bg` 丟 daemon thread 發送：呼叫端（async 主迴圈/同步收尾路徑）永不被
  網路 IO 卡住；行程結束不等未送完的通知。
- 內容只帶事件類型/任務 id/標題/一句描述，不含程式碼、log 全文或憑證。
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.request
import uuid
from pathlib import Path

from . import config, jsonl_log

log = logging.getLogger("ti.notify")


def _timeout_s() -> float:
    """送出 timeout（秒）：TI_NOTIFY_TIMEOUT 可調，reload 後即時生效。"""
    return float(getattr(config, "NOTIFY_TIMEOUT", 10.0) or 10.0)


_SENSITIVE_KEY_MARKS = ("token", "secret", "password", "authorization", "webhook")
_SENSITIVE_PATH_RE = re.compile(r"(?<![\w:/])/(?:root|home|opt|etc|var|tmp|srv)/[^\s,;]*")


def _redact_text(value: str) -> str:
    text = str(value)
    for secret in (
        config.GITHUB_TOKEN,
        config.TELEGRAM_BOT_TOKEN,
        config.NOTIFY_WEBHOOK,
    ):
        if secret:
            text = text.replace(secret, "***")
    return _SENSITIVE_PATH_RE.sub("[redacted-path]", text)[:2000]


def _safe_value(value, key: str = ""):
    if any(mark in key.lower() for mark in _SENSITIVE_KEY_MARKS):
        return {"configured": bool(value)}
    if isinstance(value, dict):
        return {str(k)[:80]: _safe_value(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_safe_value(v) for v in value[:50]]
    if isinstance(value, str):
        return _redact_text(value)
    return value


def _safe_payload(title: str, extra: dict) -> tuple[str, dict]:
    return _redact_text(title), {str(k)[:80]: _safe_value(v, str(k)) for k, v in extra.items()}


# --- 例外分級(第 3/4 階 B5) -------------------------------------------------
# page   = 立即推播:需要人採取行動的異常(送所有已設定 sink)。
# digest = 僅落檔:常態質量/回饋訊號,進 events.jsonl 由信任指標與 digest 彙整,推播只會是噪音。
# 新事件 kind 必須在此登記(守門測試 test_notify_severity 強制);未登記的 kind 一律
# 視為 page——寧可吵,不可讓新異常靜默(fail-loud)。
# watchdog_paused 的 emitter 不在本模組:外置 kill switch(deploy/ti-watchdog.sh)依
# 契約不得依賴 Python runtime,自帶 curl 推播;此處登記僅為分級口徑單一真相。
SEVERITY: dict[str, str] = {
    "task_failed": "page",
    "loop_stall": "page",
    "quota_exhausted": "page",
    "provider_unavailable": "page",
    "watchdog_paused": "page",
    "slo_brake": "page",  # A4 SLO 自動煞車
    "deploy_verify_failed": "page",  # B1 部署黑盒驗證失敗
    "clarify_pending": "page",  # B4 澄清待答
    "daily_digest": "page",  # 每日摘要(TI_DIGEST_PUSH opt-in,呼叫端擋)
    "stage_changed": "page",  # 升階狀態變化/streak 里程碑(軌 G1)
    "budget_trip": "page",  # 自治每日成本/PR 熔斷
    "policy_violation": "page",  # 來源漂移、政策拒絕或全域/per-project 煞車
    "rollback_result": "page",  # rollback 失敗必須立即通知；成功演練由呼叫端標 drill
    "ci_failed": "page",  # 本機/遠端 CI 客觀閘門重試用罄
    "manual_paused": "page",  # 人工暫停與政策 paused 都須離帶通知
    "consecutive_fail_pause": "page",  # 主迴圈連續 failed SLO 煞車暫停
    "test": "page",
    "gate_failure": "digest",
    "critic_reject": "digest",
}

# 安全的通知鏈演練：只送合成告警，不觸發對應的部署、rollback 或煞車動作。
RED_DRILL_KINDS = (
    "task_failed",
    "budget_trip",
    "quota_exhausted",
    "provider_unavailable",
    "ci_failed",
    "deploy_verify_failed",
    "rollback_result",
    "watchdog_paused",
    "manual_paused",
    "policy_violation",
    "slo_brake",
)


def severity(kind: str) -> str:
    """回傳事件分級;未登記=page(寧吵勿漏)。"""
    return SEVERITY.get(kind, "page")


def _events_path(state_dir: Path | None = None) -> Path:
    return (state_dir or config.AUTOPILOT_STATE_DIR) / "events.jsonl"


def _deliveries_path(state_dir: Path | None = None) -> Path:
    return (state_dir or config.AUTOPILOT_STATE_DIR) / "notification-deliveries.jsonl"


def _persist(kind: str, title: str, extra: dict) -> dict:
    """事件落檔（永不拋錯）；與 webhook 是否設定無關——信任指標需要無條件計數。"""
    title, extra = _safe_payload(title, extra)
    rec = {
        "event_id": uuid.uuid4().hex,
        "ts": time.time(),
        "kind": kind,
        "title": title,
        **extra,
    }
    jsonl_log.append(_events_path(), rec)
    return rec


def _persist_delivery(
    alert: dict, sink: str, ok: bool, delivery_duration_s: float, error: str = ""
) -> None:
    """投遞證據獨立落檔；latency 是事件產生到投遞完成，不只是 HTTP 呼叫耗時。"""
    try:
        alert_ts = float(alert.get("ts"))
    except (TypeError, ValueError):
        alert_ts = time.time()
    latency_s = max(0.0, time.time() - alert_ts)
    jsonl_log.append(
        _deliveries_path(),
        {
            "alert_event_id": alert.get("event_id"),
            "alert_kind": alert.get("kind"),
            "alert_ts": alert.get("ts"),
            "sink": sink,
            "ok": bool(ok),
            "latency_s": round(latency_s, 4),
            "delivery_duration_s": round(max(0.0, delivery_duration_s), 4),
            "error": error[:80],
            "drill": bool(alert.get("drill")),
        },
    )


def record(kind: str, title: str = "", **extra) -> None:
    """僅留痕不推播：內部質量事件（critic_reject/gate_failure…）進 events.jsonl。"""
    _persist(kind, title, extra)


def read_events(days: int, *, state_dir: Path | None = None) -> list[dict]:
    """讀近 days 天的事件紀錄（壞行容錯，檔案不存在=空）。"""
    return jsonl_log.read_window(_events_path(state_dir), days)


def read_deliveries(days: int, *, state_dir: Path | None = None) -> list[dict]:
    """讀近 days 天通知投遞證據；不含 URL/token/回應本文等秘密。"""
    return jsonl_log.read_window(_deliveries_path(state_dir), days)


def _post_json(url: str, payload: dict, kind: str, title: str, sink: str) -> bool:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    try:
        req = urllib.request.Request(
            url, data=body, headers={"Content-Type": "application/json"}, method="POST"
        )
        with urllib.request.urlopen(req, timeout=_timeout_s()):
            pass
        return True
    except Exception:  # noqa: BLE001 — 通知失敗不得影響呼叫端；log 不含 URL（Telegram URL 內嵌 token）
        log.debug("%s 通知送出失敗（忽略）：%s %s", sink, kind, title, exc_info=True)
        return False


def _post_webhook(url: str, kind: str, title: str, extra: dict) -> bool:
    return _post_json(
        url, {"source": "ti", "kind": kind, "title": title, **extra}, kind, title, "webhook"
    )


def _post_telegram(token: str, chat_id: str, kind: str, title: str, extra: dict) -> bool:
    """Telegram sendMessage（第 3 階 A1）：純文字（不用 parse_mode，杜絕跳脫地雷）。"""
    lines = [f"[ti] {kind}" + (f"：{title}" if title else "")]
    lines += [f"{k}={v}" for k, v in extra.items()]
    payload = {"chat_id": chat_id, "text": "\n".join(lines), "disable_web_page_preview": True}
    return _post_json(
        f"https://api.telegram.org/bot{token}/sendMessage", payload, kind, title, "telegram"
    )


def _deliver(kind: str, title: str, extra: dict, alert: dict | None = None) -> dict:
    """把事件推到所有已設定的 sink；回 {sink: 成敗}（未設定的 sink 不出現）。"""
    title, extra = _safe_payload(title, extra)
    out: dict[str, bool] = {}
    alert = alert or {"event_id": "unknown", "kind": kind, "ts": time.time()}
    url = (config.NOTIFY_WEBHOOK or "").strip()
    if url:
        started = time.monotonic()
        out["webhook"] = _post_webhook(url, kind, title, extra)
        _persist_delivery(
            alert,
            "webhook",
            out["webhook"],
            time.monotonic() - started,
            "" if out["webhook"] else "delivery_failed",
        )
    token = (config.TELEGRAM_BOT_TOKEN or "").strip()
    chat_id = (config.TELEGRAM_CHAT_ID or "").strip()
    if token and chat_id:
        started = time.monotonic()
        out["telegram"] = _post_telegram(token, chat_id, kind, title, extra)
        _persist_delivery(
            alert,
            "telegram",
            out["telegram"],
            time.monotonic() - started,
            "" if out["telegram"] else "delivery_failed",
        )
    if not out:
        _persist_delivery(alert, "none", False, 0.0, "not_configured")
    return out


def send(kind: str, title: str, **extra) -> bool:
    """同步送出一則通知（先落檔）；任一 sink 送達回 True，全失敗/皆未設定回 False。

    digest 級 kind 只落檔不推播（與 record 同效）——分級口徑見 SEVERITY。
    """
    alert = _persist(kind, title, extra)
    if severity(kind) != "page":
        return False
    return any(_deliver(kind, title, extra, alert).values())


def send_test() -> dict:
    """端到端驗證推播管道（同步）：發一則 test 事件，回報各 sink 送達狀況。"""
    alert = _persist("test", "推播管道測試", {"drill": True})
    results = _deliver("test", "推播管道測試", {}, alert)
    return {"ok": any(results.values()), "sinks": results}


def send_red_drills() -> dict:
    """同步演練所有第 3 階紅色告警；不執行事件名稱所代表的真實動作。"""
    results: dict[str, dict[str, bool]] = {}
    for kind in RED_DRILL_KINDS:
        title = f"[演練] {kind} 外部告警送達測試"
        alert = _persist(kind, title, {"drill": True})
        results[kind] = _deliver(kind, title, {"drill": True}, alert)
    return {
        "ok": bool(results) and all(any(sinks.values()) for sinks in results.values()),
        "results": results,
    }


def send_bg(kind: str, title: str, **extra) -> None:
    """背景送出（daemon thread）：呼叫端零阻塞。無任何 sink 設定時僅落檔、零網路。

    digest 級 kind 只落檔不推播——分級口徑見 SEVERITY。
    """
    alert = _persist(kind, title, extra)
    if severity(kind) != "page":
        return
    if not (
        (config.NOTIFY_WEBHOOK or "").strip()
        or ((config.TELEGRAM_BOT_TOKEN or "").strip() and (config.TELEGRAM_CHAT_ID or "").strip())
    ):
        _persist_delivery(alert, "none", False, 0.0, "not_configured")
        return
    threading.Thread(target=_deliver, args=(kind, title, extra, alert), daemon=True).start()
