"""HTTP API 路由（health、登入/登出、workspace、history、publish）。

從原本單檔 server.py 拆出，集中管理 REST 端點；需保護的端點掛上 require_auth 依賴。
WebSocket 與應用組裝分別在 ws.py / server.py。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from . import auth, backlog, config, history, publisher, redeploy, settings, workspace

router = APIRouter()

# 敏感寫入路由統一掛此依賴組，避免未來新增路由漏掛。
# 順序：require_loopback 在前 → 外網未登入請求先回 403（來源優先曝光）。
WRITE_DEPS = [Depends(auth.require_loopback), Depends(auth.require_auth)]


# --- 健康檢查 -----------------------------------------------------------
@router.get("/api/health")
async def health() -> JSONResponse:
    return JSONResponse(
        {
            "ok": True,
            "has_api_key": config.has_api_key(),
            "offline": config.OFFLINE_MODE,
            "provider": config.PROVIDER,
            "provider_ready": config.provider_ready(),
        }
    )


# --- 登入 / 門禁 --------------------------------------------------------
class LoginBody(BaseModel):
    password: str = ""


@router.get("/api/auth/status")
async def auth_status(request: Request) -> JSONResponse:
    return JSONResponse({"auth_enabled": config.auth_enabled(), "authed": auth.is_authed(request)})


@router.post("/api/login")
async def login(body: LoginBody) -> JSONResponse:
    if not config.auth_enabled():
        return JSONResponse({"ok": True, "detail": "門禁未啟用"})
    if not auth.check_password(body.password):
        return JSONResponse({"ok": False, "detail": "密碼錯誤"}, status_code=401)
    response = JSONResponse({"ok": True})
    response.set_cookie(
        config.AUTH_COOKIE,
        auth.make_token(),
        max_age=config.AUTH_TTL,
        httponly=True,
        samesite="lax",
    )
    return response


@router.post("/api/logout")
async def logout() -> JSONResponse:
    response = JSONResponse({"ok": True})
    response.delete_cookie(config.AUTH_COOKIE)
    return response


class PasswordBody(BaseModel):
    current_password: str = ""
    new_password: str = ""


@router.post("/api/auth/password", dependencies=WRITE_DEPS)
async def change_password(body: PasswordBody) -> JSONResponse:
    """變更 / 設定存取密碼。

    - 門禁已啟用：require_auth 確保已登入，再驗證『目前密碼』正確才放行。
    - 門禁未啟用：可直接設定一組新密碼以首次啟用門禁（此時無需目前密碼）。
    成功後回應會附上新的登入 cookie，避免操作者在啟用門禁的當下被登出。
    """
    if config.auth_enabled() and not auth.check_password(body.current_password):
        return JSONResponse({"ok": False, "detail": "目前密碼錯誤"}, status_code=403)
    new = (body.new_password or "").strip()
    if len(new) < 4:
        return JSONResponse({"ok": False, "detail": "新密碼至少 4 個字元"}, status_code=400)
    auth.set_password(new)
    response = JSONResponse({"ok": True, "auth_enabled": config.auth_enabled()})
    response.set_cookie(
        config.AUTH_COOKIE,
        auth.make_token(),
        max_age=config.AUTH_TTL,
        httponly=True,
        samesite="lax",
    )
    return response


# --- 設定（受保護）----------------------------------------------------
@router.get("/api/settings", dependencies=[Depends(auth.require_auth)])
async def get_settings() -> JSONResponse:
    return JSONResponse(settings.read())


@router.post("/api/settings", dependencies=WRITE_DEPS)
async def post_settings(request: Request) -> JSONResponse:
    body = await request.json()
    if not isinstance(body, dict):
        return JSONResponse({"ok": False, "detail": "格式錯誤"}, status_code=400)
    return JSONResponse({"ok": True, **settings.update(body)})


# --- workspace（受保護）------------------------------------------------
@router.get("/api/workspace/{session_id}/files", dependencies=[Depends(auth.require_auth)])
async def workspace_files(session_id: str) -> JSONResponse:
    return JSONResponse({"files": workspace.list_files(session_id)})


@router.get("/api/workspace/{session_id}/file", dependencies=[Depends(auth.require_auth)])
async def workspace_file(session_id: str, path: str) -> JSONResponse:
    content = workspace.read_file(session_id, path)
    if content is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse({"path": path, "content": content})


@router.get("/api/workspace/{session_id}/download", dependencies=[Depends(auth.require_auth)])
async def workspace_download(session_id: str) -> Response:
    data = workspace.zip_workspace(session_id)
    if data is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    # 檔名只保留安全字元，避免 header injection 並讓 session_id 可辨識。
    safe = "".join(c for c in session_id if c.isalnum() or c in "-_") or "workspace"
    return Response(
        content=data,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="workspace-{safe}.zip"'},
    )


# --- history（受保護）--------------------------------------------------
@router.get("/api/history", dependencies=[Depends(auth.require_auth)])
async def history_list() -> JSONResponse:
    return JSONResponse({"sessions": history.list_sessions()})


@router.get("/api/history/{session_id}/events", dependencies=[Depends(auth.require_auth)])
async def history_events(session_id: str) -> JSONResponse:
    meta = history.get_meta(session_id)
    if meta is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse({"meta": meta, "events": history.load_events(session_id)})


@router.delete("/api/history/{session_id}", dependencies=[Depends(auth.require_auth)])
async def history_delete(session_id: str) -> JSONResponse:
    ok = history.delete_session(session_id)
    return JSONResponse({"ok": ok}, status_code=200 if ok else 404)


@router.post("/api/history/cleanup/completed", dependencies=[Depends(auth.require_auth)])
async def history_cleanup_completed() -> JSONResponse:
    return JSONResponse({"deleted": history.delete_completed_sessions()})


# --- publish（受保護）--------------------------------------------------
@router.get("/api/publish/config", dependencies=[Depends(auth.require_auth)])
async def publish_config() -> JSONResponse:
    return JSONResponse(
        {
            "configured": publisher.is_configured(),
            "auto": config.PUBLISH_AUTO,
            "merge": config.PUBLISH_MERGE,
            "repo": config.PUBLISH_REPO or None,
        }
    )


@router.post("/api/publish/{session_id}", dependencies=[Depends(auth.require_auth)])
async def publish_now(session_id: str) -> JSONResponse:
    cwd = workspace.workspace_path(session_id)
    if not cwd.exists():
        return JSONResponse(
            {"ok": False, "detail": "找不到此 session 的 workspace"}, status_code=404
        )
    meta = history.get_meta(session_id)
    requirement = meta["requirement"] if meta else "Ti Studio 成果"
    # 手動發佈：session 已結束、團隊已散，無法自我修復；走 publish(merge=) 一次性「等 CI→合併」，
    # 結局（outcome）寫進 to_dict 供前端徽章顯示，不另起修正迴圈。
    result = await publisher.publish(cwd, session_id, requirement, merge=config.PUBLISH_MERGE)
    return JSONResponse(result.to_dict())


# --- 重新佈署重啟（受保護）--------------------------------------------
@router.post("/api/redeploy", dependencies=WRITE_DEPS)
async def redeploy_now() -> JSONResponse:
    """拉取主 repo 最新 main 並自我重啟，讓合併後的新程式碼生效。"""
    result = await redeploy.redeploy()
    return JSONResponse(result)


# --- autopilot（受保護）------------------------------------------------
class TaskBody(BaseModel):
    title: str = ""
    detail: str = ""


@router.get("/api/autopilot", dependencies=[Depends(auth.require_auth)])
async def autopilot_status() -> JSONResponse:
    return JSONResponse(
        {
            "paused": config.autopilot_paused(),
            "counts": backlog.counts(),
            "dryrun": config.AUTOPILOT_DRYRUN,
            "repo": config.AUTOPILOT_REPO,
        }
    )


@router.get("/api/autopilot/backlog", dependencies=[Depends(auth.require_auth)])
async def autopilot_backlog() -> JSONResponse:
    return JSONResponse({"tasks": backlog.list_tasks()})


@router.post("/api/autopilot/pause", dependencies=WRITE_DEPS)
async def autopilot_pause() -> JSONResponse:
    config.AUTOPILOT_PAUSE_FILE.write_text("paused via UI\n", encoding="utf-8")
    return JSONResponse({"ok": True, "paused": True})


@router.post("/api/autopilot/resume", dependencies=WRITE_DEPS)
async def autopilot_resume() -> JSONResponse:
    config.AUTOPILOT_PAUSE_FILE.unlink(missing_ok=True)
    return JSONResponse({"ok": True, "paused": config.autopilot_paused()})


@router.post("/api/autopilot/task", dependencies=WRITE_DEPS)
async def autopilot_add_task(body: TaskBody) -> JSONResponse:
    task = backlog.add(body.title, body.detail, source="manual")
    if task is None:
        return JSONResponse({"ok": False, "detail": "標題為空或已存在"}, status_code=400)
    return JSONResponse({"ok": True, "task": task})
