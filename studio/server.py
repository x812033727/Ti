"""應用組裝：建立 FastAPI app、掛載靜態檔與路由、提供頁面入口與啟動函式。

REST 路由在 routes.py、WebSocket 在 ws.py、認證在 auth.py。
保留 `studio.server:app` 與 `python -m studio.server` 入口不變。
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import auth, config, routes, ws

app = FastAPI(title="Ti Studio — AI 專家討論工作室")

if config.WEB_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(config.WEB_DIR)), name="static")

app.include_router(routes.router)
app.include_router(ws.router)


@app.get("/")
async def index(request: Request) -> FileResponse:
    # 門禁啟用且尚未登入時，導向登入頁。
    if config.auth_enabled() and not auth.is_authed(request):
        return FileResponse(str(config.WEB_DIR / "login.html"))
    return FileResponse(str(config.WEB_DIR / "index.html"))


@app.get("/login")
async def login_page() -> FileResponse:
    return FileResponse(str(config.WEB_DIR / "login.html"))


def main() -> None:
    import uvicorn

    uvicorn.run(app, host=config.HOST, port=config.PORT)


if __name__ == "__main__":
    main()
