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
import threading
import urllib.request
from pathlib import Path

from . import config, jsonl_log

log = logging.getLogger("ti.notify")

_TIMEOUT_S = 10.0


def _events_path(state_dir: Path | None = None) -> Path:
    return (state_dir or config.AUTOPILOT_STATE_DIR) / "events.jsonl"


def _persist(kind: str, title: str, extra: dict) -> None:
    """事件落檔（永不拋錯）；與 webhook 是否設定無關——信任指標需要無條件計數。"""
    jsonl_log.append(_events_path(), {"kind": kind, "title": title, **extra})


def record(kind: str, title: str = "", **extra) -> None:
    """僅留痕不推播：內部質量事件（critic_reject/gate_failure…）進 events.jsonl。"""
    _persist(kind, title, extra)


def read_events(days: int, *, state_dir: Path | None = None) -> list[dict]:
    """讀近 days 天的事件紀錄（壞行容錯，檔案不存在=空）。"""
    return jsonl_log.read_window(_events_path(state_dir), days)


def _post_json(url: str, payload: dict, kind: str, title: str, sink: str) -> bool:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    try:
        req = urllib.request.Request(
            url, data=body, headers={"Content-Type": "application/json"}, method="POST"
        )
        with urllib.request.urlopen(req, timeout=_TIMEOUT_S):
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


def _deliver(kind: str, title: str, extra: dict) -> dict:
    """把事件推到所有已設定的 sink；回 {sink: 成敗}（未設定的 sink 不出現）。"""
    out: dict[str, bool] = {}
    url = (config.NOTIFY_WEBHOOK or "").strip()
    if url:
        out["webhook"] = _post_webhook(url, kind, title, extra)
    token = (config.TELEGRAM_BOT_TOKEN or "").strip()
    chat_id = (config.TELEGRAM_CHAT_ID or "").strip()
    if token and chat_id:
        out["telegram"] = _post_telegram(token, chat_id, kind, title, extra)
    return out


def send(kind: str, title: str, **extra) -> bool:
    """同步送出一則通知（先落檔）；任一 sink 送達回 True，全失敗/皆未設定回 False。"""
    _persist(kind, title, extra)
    return any(_deliver(kind, title, extra).values())


def send_test() -> dict:
    """端到端驗證推播管道（同步）：發一則 test 事件，回報各 sink 送達狀況。"""
    _persist("test", "推播管道測試", {})
    results = _deliver("test", "推播管道測試", {})
    return {"ok": any(results.values()), "sinks": results}


def send_bg(kind: str, title: str, **extra) -> None:
    """背景送出（daemon thread）：呼叫端零阻塞。無任何 sink 設定時僅落檔、零網路。"""
    _persist(kind, title, extra)
    if not (
        (config.NOTIFY_WEBHOOK or "").strip()
        or ((config.TELEGRAM_BOT_TOKEN or "").strip() and (config.TELEGRAM_CHAT_ID or "").strip())
    ):
        return
    threading.Thread(target=_deliver, args=(kind, title, extra), daemon=True).start()
