"""WebSocket 端點：即時串流專家討論，並接收人類插話 / 停止指令。

從原本單檔 server.py 拆出。門禁啟用時，握手後會先檢查登入 cookie。
"""

from __future__ import annotations

import asyncio
import uuid

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from . import auth, config, events, history, runner, workspace
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
    connected = True

    async def broadcast(event: StudioEvent) -> None:
        nonlocal connected
        d = event.to_dict()
        if recording:
            history.record_event(session_id, d)
        # 客戶端可能在討論進行中斷線（關分頁／網路斷）。連線已關後若再 send_json
        # 會丟 RuntimeError("websocket.send after close")。一旦偵測到關閉就停止再送，
        # 歷史仍照常記錄，事件不會遺失。
        if not connected:
            return
        try:
            await websocket.send_json(d)
        except (RuntimeError, WebSocketDisconnect):
            connected = False

    try:
        # 第一則訊息為產品需求（可選擇附帶要 clone 的 GitHub repo）
        data = await websocket.receive_json()
        requirement = (data.get("requirement") or "").strip()
        repo_url = (data.get("repo_url") or "").strip()
        repo_branch = (data.get("repo_branch") or "").strip() or None
        if not requirement:
            await websocket.send_json({"type": "error", "payload": {"message": "需求不可為空"}})
            await websocket.close()
            return

        # 離線示範用腳本化假專家，會自己寫檔，因此忽略 repo（避免衝突）。
        if repo_url and config.OFFLINE_MODE:
            repo_url = ""
        if repo_url and not runner.is_valid_repo_url(repo_url):
            await websocket.send_json(
                {
                    "type": "error",
                    "payload": {
                        "message": "GitHub repo 網址無效（僅支援 github.com 的 https 網址）"
                    },
                }
            )
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

        # 若指定了 GitHub repo，先 clone 進 workspace，讓專家在現有程式碼上討論/修改。
        if repo_url:
            await broadcast(events.phase_change(session_id, "準備", f"正在 clone {repo_url} …"))
            clone = await runner.git_clone(
                repo_url, cwd, token=config.GITHUB_TOKEN, branch=repo_branch
            )
            if not clone.ok:
                await websocket.send_json(
                    {"type": "error", "payload": {"message": "clone 失敗：" + clone.output[:500]}}
                )
                await websocket.close()
                return

        history.start_session(session_id, requirement)
        recording = True
        queue: asyncio.Queue[str] = asyncio.Queue()
        experts = None
        if config.OFFLINE_MODE:
            from .fake_experts import build_fake_experts

            experts = build_fake_experts(session_id, cwd, requirement)
        session = StudioSession(
            session_id,
            broadcast,
            experts=experts,
            cwd=cwd,
            intervention_queue=queue,
            repo_url=repo_url or None,
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
            # 客戶端斷線（重整頁面／關分頁／網路斷）時，不要把討論當成「停止」。
            # 讓編排在背景繼續跑到完成，事件照常寫進 history（broadcast 對已關連線會
            # 安靜略過），使用者事後可從歷史看完整結果。只有前端明確送 stop 才中止。
            break
        kind = msg.get("type")
        if kind == "interject":
            text = (msg.get("text") or "").strip()
            if text:
                queue.put_nowait(text)
        elif kind == "stop":
            session.request_stop()
            break
