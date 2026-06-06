"""FastAPI 伺服器：提供工作室網頁 UI，並透過 WebSocket 即時串流專家討論。"""

from __future__ import annotations

import asyncio
import uuid

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import config, history, publisher, workspace
from .events import StudioEvent
from .orchestrator import StudioSession

app = FastAPI(title="Ti Studio — AI 專家討論工作室")

if config.WEB_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(config.WEB_DIR)), name="static")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(str(config.WEB_DIR / "index.html"))


@app.get("/api/health")
async def health() -> JSONResponse:
    return JSONResponse({
        "ok": True,
        "has_api_key": config.has_api_key(),
        "offline": config.OFFLINE_MODE,
    })


@app.get("/api/workspace/{session_id}/files")
async def workspace_files(session_id: str) -> JSONResponse:
    return JSONResponse({"files": workspace.list_files(session_id)})


@app.get("/api/workspace/{session_id}/file")
async def workspace_file(session_id: str, path: str) -> JSONResponse:
    content = workspace.read_file(session_id, path)
    if content is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse({"path": path, "content": content})


@app.get("/api/history")
async def history_list() -> JSONResponse:
    return JSONResponse({"sessions": history.list_sessions()})


@app.get("/api/history/{session_id}/events")
async def history_events(session_id: str) -> JSONResponse:
    meta = history.get_meta(session_id)
    if meta is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse({"meta": meta, "events": history.load_events(session_id)})


@app.get("/api/publish/config")
async def publish_config() -> JSONResponse:
    return JSONResponse(
        {"configured": publisher.is_configured(), "auto": config.PUBLISH_AUTO,
         "repo": config.PUBLISH_REPO or None}
    )


@app.post("/api/publish/{session_id}")
async def publish_now(session_id: str) -> JSONResponse:
    cwd = workspace.workspace_path(session_id)
    if not cwd.exists():
        return JSONResponse({"ok": False, "detail": "找不到此 session 的 workspace"}, status_code=404)
    meta = history.get_meta(session_id)
    requirement = meta["requirement"] if meta else "Ti Studio 成果"
    result = await publisher.publish(cwd, session_id, requirement)
    return JSONResponse(result.to_dict())


@app.websocket("/ws")
async def ws(websocket: WebSocket) -> None:
    await websocket.accept()
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

        if not config.has_api_key() and not config.OFFLINE_MODE:
            await websocket.send_json(
                {"type": "error", "payload": {"message": "未設定 ANTHROPIC_API_KEY，無法啟動專家"}}
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
        done, _pending = await asyncio.wait(
            {recv, run_task}, return_when=asyncio.FIRST_COMPLETED
        )
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


def main() -> None:
    import uvicorn

    uvicorn.run(app, host=config.HOST, port=config.PORT)


if __name__ == "__main__":
    main()
