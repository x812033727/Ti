"""非阻塞通知介面。

未設定 webhook 時只留 log；有 webhook 時以 daemon thread 背景送出 JSON，避免卡住主流程。
"""

from __future__ import annotations

import json
import logging
import threading
import urllib.request

from . import config

log = logging.getLogger("ti.notify")


def _post_webhook(
    webhook: str,
    timeout: float,
    event: str,
    message: str,
    payload: dict[str, object],
) -> None:
    try:
        body = json.dumps(
            {"event": event, "message": message, "payload": payload},
            ensure_ascii=False,
            default=str,
        ).encode("utf-8")
        request = urllib.request.Request(
            webhook,
            data=body,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=timeout):  # noqa: S310
            pass
    except Exception:
        return


def _deliver(event: str, message: str, payload: dict[str, object]) -> None:
    """送出背景通知；payload 不應包含憑證或完整 log。"""
    webhook = config.NOTIFY_WEBHOOK
    if not webhook:
        return
    _post_webhook(webhook, float(config.NOTIFY_TIMEOUT), event, message, payload)


def send_bg(event: str, message: str, **payload) -> None:
    """背景通知入口；未設定 sink 時只留 log，不阻塞主流程。"""
    log.info("notify %s: %s %s", event, message, payload)
    webhook = config.NOTIFY_WEBHOOK
    if not webhook:
        return

    thread = threading.Thread(
        target=_deliver,
        args=(event, message, dict(payload)),
        daemon=True,
    )
    thread.start()
