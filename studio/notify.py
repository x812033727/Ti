"""主動通知 webhook（功能第五輪 F2）：異常事件 POST 到使用者自設端點。

全系統原本零主動通知——任務失敗/額度耗盡/主迴圈停滯都要開面板輪詢才知道。
設 TI_NOTIFY_WEBHOOK（空=關，預設）後，關鍵異常會 POST 一筆 JSON：
    {"source": "ti", "kind": "<事件類型>", "title": "<一句人話>", ...extra}
端點自理路由（Slack/Discord/自架皆可作 relay）。

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

from . import config

log = logging.getLogger("ti.notify")

_TIMEOUT_S = 10.0


def send(kind: str, title: str, **extra) -> bool:
    """同步送出一則通知；未設 webhook 回 False，任何失敗吞掉回 False。"""
    url = (config.NOTIFY_WEBHOOK or "").strip()
    if not url:
        return False
    body = json.dumps(
        {"source": "ti", "kind": kind, "title": title, **extra}, ensure_ascii=False
    ).encode("utf-8")
    try:
        req = urllib.request.Request(
            url, data=body, headers={"Content-Type": "application/json"}, method="POST"
        )
        with urllib.request.urlopen(req, timeout=_TIMEOUT_S):
            pass
        return True
    except Exception:  # noqa: BLE001 — 通知失敗不得影響呼叫端
        log.debug("webhook 通知送出失敗（忽略）：%s %s", kind, title, exc_info=True)
        return False


def send_bg(kind: str, title: str, **extra) -> None:
    """背景送出（daemon thread）：呼叫端零阻塞。未設 webhook 時零成本直接返回。"""
    if not (config.NOTIFY_WEBHOOK or "").strip():
        return
    threading.Thread(target=send, args=(kind, title), kwargs=extra, daemon=True).start()
