"""任務 #5 QA 冒煙：真實啟動 `python -m studio.server`（非 TestClient），fake experts 走
「需求→議程拆解→分派→逐子題討論→彙整」全流程。

由 QA 獨立撰寫，與工程師的 in-process e2e（tests/test_offline_agenda_e2e.py）互補：
此處走真 uvicorn＋真 TCP＋真 WebSocket，對應驗收標準 7「真實啟動 server」。

用法（由外層先以 TI_OFFLINE=1 等 env 背景啟動 server）：
    python3 tests/server/smoke_agenda_real_server.py <port>
退出碼 0=PASS、1=FAIL（逐項列出）。
"""

from __future__ import annotations

import asyncio
import json
import sys

import httpx
from _loopback_clients import loopback_websocket_connect

REQUIREMENT = "做一個四則運算 CLI"
# fake PM 循序腳本宣告的議程子題（studio/fake_experts.py _pm_decompose_script）。
EXPECTED_TITLES = ["核心運算模組", "介面與說明"]

FAILS: list[str] = []


def check(cond: bool, label: str) -> None:
    print(("PASS" if cond else "FAIL"), "-", label)
    if not cond:
        FAILS.append(label)


async def main(port: int) -> int:
    base = f"http://127.0.0.1:{port}"
    evs: list[dict] = []
    async with loopback_websocket_connect(f"ws://127.0.0.1:{port}/ws", max_size=2**22) as ws:
        await ws.send(json.dumps({"requirement": REQUIREMENT}))
        for _ in range(800):
            ev = json.loads(await asyncio.wait_for(ws.recv(), timeout=60))
            evs.append(ev)
            if ev["type"] in ("done", "error"):
                break

    by: dict[str, list[dict]] = {}
    for e in evs:
        by.setdefault(e["type"], []).append(e)

    # 0) 全流程無未捕捉例外、驗收通過
    check("error" not in by, "全流程無 error 事件")
    done = by.get("done", [{}])[-1]
    check(bool(done) and done.get("payload", {}).get("completed") is True, "done.completed=True")
    sid = done.get("session_id", "")

    # 1) 議程拆解＋分派：agenda_plan 回指本場 fake PM 輸入（自證對應，排除假綠）
    plans = by.get("agenda_plan", [])
    check(len(plans) == 1, "恰一筆 agenda_plan 事件")
    plan = plans[0]["payload"] if plans else {}
    titles = [a.get("title") for a in plan.get("agenda", [])]
    check(titles == EXPECTED_TITLES, f"議程標題回指 PM 腳本: {titles}")
    pm_texts = [
        e["payload"]["text"]
        for e in by.get("expert_message", [])
        if e["payload"].get("speaker") == "pm"
    ]
    check(
        all(any(t in txt for txt in pm_texts) for t in EXPECTED_TITLES),
        "子題標題確實出現在本場 PM 發言全文中（非 parser 憑空生出）",
    )

    # 2) 分派硬驗證：engineer 合法照派；architect 缺席 → fallback engineer ＋ corrections
    assignees = [a.get("assignee") for a in plan.get("assignments", [])]
    check(assignees == ["engineer", "engineer"], f"assignee 硬驗證後皆 engineer: {assignees}")
    check(
        plan.get("corrections") == [{"index": 1, "given": "architect", "assigned": "engineer"}],
        f"非法 key(architect) 修正紀錄入事件: {plan.get('corrections')}",
    )
    check(len(plan.get("tasks", [])) == 3, "任務清單 3 筆（沿用既有 parse_tasks）")

    # 3) 逐子題討論真的發生（引擎模式）
    phases = [
        (e["payload"].get("phase"), e["payload"].get("detail", ""))
        for e in by.get("phase_change", [])
    ]
    check(
        any(p == "架構討論" and "逐子題" in d and "2 個子題" in d for p, d in phases),
        "phase_change 含「逐子題多角色討論（2 個子題）」",
    )
    try:
        idx = next(
            i
            for i, e in enumerate(evs)
            if e["type"] == "phase_change" and e["payload"].get("phase") == "架構討論"
        )
        nxt = next(i for i, e in enumerate(evs) if i > idx and e["type"] == "phase_change")
        speakers = [e["payload"]["speaker"] for e in evs[idx:nxt] if e["type"] == "expert_message"]
    except StopIteration:
        speakers = []
    check(
        speakers.count("engineer") == 2 and speakers.count("senior") == 2,
        f"逐子題討論期間 engineer/senior 各發言 2 次（2 子題×1 輪）: {speakers}",
    )

    # 4) 彙整零回歸：任務全完成、demo 通過
    done_ids = {
        e["payload"]["id"]
        for e in by.get("task_status", [])
        if e["payload"].get("status") == "done"
    }
    check(len(done_ids) == 3, f"3 個任務全完成: {sorted(done_ids)}")
    demos = by.get("demo_result", [])
    check(
        bool(demos)
        and demos[-1]["payload"].get("passed") is True
        and "7.0" in demos[-1]["payload"].get("output", ""),
        "Demo 通過且輸出含 7.0",
    )

    # 5) 可重看：HTTP history API 回放含同一筆 agenda_plan
    # trust_env=False：宿主可能設 SOCKS/HTTP proxy env，本機 loopback 冒煙絕不走 proxy。
    async with httpx.AsyncClient(trust_env=False) as hc:
        r = await hc.get(f"{base}/api/history/{sid}/events")
        replay = r.json().get("events", []) if r.status_code == 200 else []
    saved = [e for e in replay if e["type"] == "agenda_plan"]
    check(len(replay) == len(evs), f"history 重播事件數一致: {len(replay)} vs {len(evs)}")
    check(len(saved) == 1 and saved[0]["payload"] == plan, "history 中 agenda_plan 與現場一致")

    print()
    if FAILS:
        print(f"SMOKE FAIL ({len(FAILS)}):")
        for f in FAILS:
            print(" -", f)
        return 1
    print("SMOKE PASS (all checks)")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(int(sys.argv[1]))))
