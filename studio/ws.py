"""WebSocket 端點：即時串流專家討論，並接收人類插話 / 停止指令。

從原本單檔 server.py 拆出。門禁啟用時，握手後會先檢查登入 cookie。
"""

from __future__ import annotations

import asyncio
import uuid

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from . import auth, config, history, workspace
from .events import StudioEvent
from .orchestrator import StudioSession

router = APIRouter()


@router.websocket("/ws")
async def ws(websocket: WebSocket) -> None:
    await websocket.accept()

    # 門禁啟用時，未登入的連線直接拒絕。
    if not auth.is_authed(websocket):
        await websocket.send_json(
            {"type": "error", "payload": {"message": "需要登入後才能啟動工作室"}}
        )
        await websocket.close(code=1008)
        return

    session_id = uuid.uuid4().hex[:12]
    recording = False

    async def broadcast(event: StudioEvent) -> None:
        d = event.to_dict()
        if recording:
            history.record_event(session_id, d)
        await websocket.send_json(d)

    try:
        # 第一則訊息為產品需求
        data = await websocket.receive_json()
        requirement = (data.get("requirement") or "").strip()
        if not requirement:
            await websocket.send_json({"type": "error", "payload": {"message": "需求不可為空"}})
            await websocket.close()
            return

        if not config.provider_ready() and not config.OFFLINE_MODE:
            hint = (
                "未設定 OPENAI_API_KEY / OPENAI_BASE_URL"
                if config.PROVIDER == "openai"
                else "未設定 ANTHROPIC_API_KEY"
            )
            await websocket.send_json(
                {
                    "type": "error",
                    "payload": {"message": f"{hint}，無法啟動專家（或用 TI_OFFLINE=1 試用）"},
                }
            )
            await websocket.close()
            return

        cwd = workspace.create_workspace(session_id)
        history.start_session(session_id, requirement)
        recording = True
        queue: asyncio.Queue[str] = asyncio.Queue()
        experts = None
        if config.OFFLINE_MODE:
            from .fake_experts import build_fake_experts

            experts = build_fake_experts(session_id, cwd, requirement)
        session = StudioSession(
            session_id, broadcast, experts=experts, cwd=cwd, intervention_queue=queue
        )

        # 編排在背景跑，主迴圈同時接收人類插話 / 停止指令
        run_task = asyncio.create_task(session.run(requirement))
        await _pump_interventions(websocket, session, queue, run_task)
        await run_task
    except WebSocketDisconnect:
        pass
    finally:
        if recording:
            history.finish_session(session_id)
        try:
            await websocket.close()
        except RuntimeError:
            pass


async def _pump_interventions(websocket, session, queue, run_task) -> None:
    """編排執行期間，持續接收前端訊息並注入 session（插話 / 停止）。"""
    while not run_task.done():
        recv = asyncio.ensure_future(websocket.receive_json())
        done, _pending = await asyncio.wait({recv, run_task}, return_when=asyncio.FIRST_COMPLETED)
        if recv not in done:
            recv.cancel()
            break
        try:
            msg = recv.result()
        except WebSocketDisconnect:
            session.request_stop()
            break
        kind = msg.get("type")
        if kind == "interject":
            text = (msg.get("text") or "").strip()
            if text:
                queue.put_nowait(text)
        elif kind == "stop":
            session.request_stop()
            break
