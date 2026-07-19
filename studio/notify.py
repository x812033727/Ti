"""非阻塞通知介面。

目前 workspace 版沒有外部通知 sink；保留 send_bg 合約，讓 autopilot 關鍵事件可被測試與後續接線。
"""

from __future__ import annotations

import logging

log = logging.getLogger("ti.notify")


def send_bg(event: str, message: str, **payload) -> None:
    """背景通知入口；未設定 sink 時只留 log，不阻塞主流程。"""
    log.info("notify %s: %s %s", event, message, payload)
