"""FastAPI 伺服器：提供工作室網頁 UI，並透過 WebSocket 即時串流專家討論。"""

from __future__ import annotations

import uuid

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import config, workspace
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
    return JSONResponse({"ok": True, "has_api_key": config.has_api_key()})


@app.get("/api/workspace/{session_id}/files")
async def workspace_files(session_id: str) -> JSONResponse:
    return JSONResponse({"files": workspace.list_files(session_id)})


@app.get("/api/workspace/{session_id}/file")
async def workspace_file(session_id: str, path: str) -> JSONResponse:
    content = workspace.read_file(session_id, path)
    if content is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse({"path": path, "content": content})


@app.websocket("/ws")
async def ws(websocket: WebSocket) -> None:
    await websocket.accept()
    session_id = uuid.uuid4().hex[:12]

    async def broadcast(event: StudioEvent) -> None:
        await websocket.send_json(event.to_dict())

    try:
        # 第一則訊息為產品需求
        data = await websocket.receive_json()
        requirement = (data.get("requirement") or "").strip()
        if not requirement:
            await websocket.send_json({"type": "error", "payload": {"message": "需求不可為空"}})
            await websocket.close()
            return

        if not config.has_api_key():
            await websocket.send_json(
                {"type": "error", "payload": {"message": "未設定 ANTHROPIC_API_KEY，無法啟動專家"}}
            )
            await websocket.close()
            return

        cwd = workspace.create_workspace(session_id)
        session = StudioSession(session_id, broadcast, cwd=cwd)
        await session.run(requirement)
    except WebSocketDisconnect:
        pass
    finally:
        try:
            await websocket.close()
        except RuntimeError:
            pass


def main() -> None:
    import uvicorn

    uvicorn.run(app, host=config.HOST, port=config.PORT)


if __name__ == "__main__":
    main()
