"""應用組裝：建立 FastAPI app、掛載靜態檔與路由、提供頁面入口與啟動函式。

REST 路由在 routes.py、WebSocket 在 ws.py、認證在 auth.py。
保留 `studio.server:app` 與 `python -m studio.server` 入口不變。
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import auth, config, history, routes, ws

# 沙箱啟用但缺依賴時 CLI 會靜默 fail-open（無沙箱執行），啟動時大聲示警。
_sandbox_missing = config.sandbox_missing_deps()
if _sandbox_missing:
    logging.getLogger("ti.sandbox").warning(
        "⚠️ 沙箱已啟用(TI_SANDBOX)但缺少 %s：專家 bash 會在【無沙箱】下以 root 執行"
        "(CLI fail-open)。請 `apt install bubblewrap socat`，或設 TI_SANDBOX=0 明確關閉。",
        ", ".join(_sandbox_missing),
    )


@asynccontextmanager
async def _lifespan(app: FastAPI):
    # 啟動時掃一次 history 保留策略：把「停用回收期間／升級前」累積的舊 session 立即壓回上限內
    # （session 收尾時也會各自掃一次）。失敗絕不可擋住服務啟動。
    try:
        history.enforce_retention()
    except Exception:  # noqa: BLE001
        logging.getLogger("ti.history").warning("啟動回收失敗（略過，不影響啟動）", exc_info=True)
    yield


app = FastAPI(title="Ti Studio — AI 專家討論工作室", lifespan=_lifespan)

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

    # proxy_headers + forwarded_allow_ips：讓 uvicorn 的 ProxyHeadersMiddleware 僅採信
    # 受信來源（預設本機）送來的 X-Forwarded-*，關閉「取最左值偽造 client IP」攻擊面
    # （issue #0001）。forwarded_allow_ips() 偵測到 "*" 會在此 fail-closed 拒啟動。
    uvicorn.run(
        app,
        host=config.HOST,
        port=config.PORT,
        proxy_headers=True,
        forwarded_allow_ips=config.forwarded_allow_ips(),
    )


if __name__ == "__main__":
    main()
