"""WS 斷線重掛（attach）真實 server 冒煙：真 uvicorn＋真 TCP＋真斷線。

TestClient 每條 WS 連線各自一個 portal/事件迴圈，連線關閉即拆迴圈——「斷線後背景
續跑（detach）」在 TestClient 下天然測不到，故此情境走真實 server（樣板同
smoke_agenda_real_server.py）：

  1. ws1 開場離線討論，收前幾筆事件（記 cursor）後「硬斷線」；
  2. 立即以 ws2 `{"attach": sid, "cursor": n}` 重掛：收補放＋attach_ok＋live；
  3. 從 ws2 插話，斷言 human_message 回顯出現在串流；
  4. 收到 done 後，向 /api/history/{sid}/events 取全量 JSONL，斷言
     「ws1 收到的 + ws2 收到的（去 attach_ok）」與 JSONL 逐筆相等——跨斷線無重複無遺漏。

用法（由外層先以 TI_OFFLINE=1 等 env 背景啟動 server）：
    python3 tests/server/smoke_ws_attach_real_server.py <port>
退出碼 0=PASS、1=FAIL（逐項列出）。
"""

from __future__ import annotations

import asyncio
import json
import sys

import httpx
from _loopback_clients import loopback_websocket_connect

REQUIREMENT = "做一個 BMI CLI"

FAILS: list[str] = []


def check(cond: bool, label: str) -> None:
    print(("PASS" if cond else "FAIL"), "-", label)
    if not cond:
        FAILS.append(label)


async def main(port: int) -> int:
    base = f"http://127.0.0.1:{port}"
    url = f"ws://127.0.0.1:{port}/ws"
    got1: list[dict] = []
    sid = ""

    # 1) 開場，收前幾筆後硬斷線（不送 stop——斷線不可等於停止）
    ws1 = await loopback_websocket_connect(url, max_size=2**22)
    await ws1.send(json.dumps({"requirement": REQUIREMENT}))
    for _ in range(6):
        ev = json.loads(await asyncio.wait_for(ws1.recv(), timeout=60))
        got1.append(ev)
        if not sid and ev.get("session_id"):
            sid = ev["session_id"]
    await ws1.close()
    check(bool(sid), "ws1 取得 session_id")
    check(got1[0]["type"] == "session_started", "第一筆事件為 session_started（cursor 起算基準）")
    cursor = len(got1)

    # 2) 立即重掛：補放 + attach_ok + live
    got2: list[dict] = []
    saw_attach_ok = False
    interjected = False
    async with loopback_websocket_connect(url, max_size=2**22) as ws2:
        await ws2.send(json.dumps({"attach": sid, "cursor": cursor}))
        for _ in range(3000):
            ev = json.loads(await asyncio.wait_for(ws2.recv(), timeout=60))
            if ev.get("type") == "attach_ok":
                saw_attach_ok = True
                check(
                    ev["payload"].get("cursor", 0) >= cursor,
                    "attach_ok 回權威計數（>= 前端 cursor）",
                )
                continue
            got2.append(ev)
            if saw_attach_ok and not interjected:
                interjected = True
                await ws2.send(json.dumps({"type": "interject", "text": "重掛端插話測試"}))
            if ev.get("type") == "done":
                break
    check(saw_attach_ok, "收到 attach_ok")
    check(got2 and got2[-1]["type"] == "done", "attach 端收到收尾 done（斷線後背景續跑到完成）")
    check(
        any(
            e["type"] == "human_message" and "重掛端插話測試" in (e["payload"].get("text") or "")
            for e in got2
        ),
        "attach 端插話的 human_message 回顯出現在串流",
    )

    # 3) 全量對帳：ws1 + ws2 == JSONL（跨斷線無重複無遺漏）
    # trust_env=False：宿主可能設 SOCKS/HTTP proxy env，本機 loopback 冒煙絕不走 proxy。
    async with httpx.AsyncClient(trust_env=False) as http:
        res = await http.get(f"{base}/api/history/{sid}/events", timeout=30)
        full = res.json().get("events", [])
    stitched = got1 + got2
    check(len(stitched) == len(full), f"事件總數對帳（拼接 {len(stitched)} vs JSONL {len(full)}）")
    check(
        all(a == b for a, b in zip(stitched, full, strict=False)),
        "拼接序列與 JSONL 逐筆相等（無重複無遺漏）",
    )

    print("SMOKE PASS" if not FAILS else f"SMOKE FAIL: {FAILS}")
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main(int(sys.argv[1]))))
