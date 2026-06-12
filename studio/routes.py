"""HTTP API 路由（health、登入/登出、workspace、history、publish）。

從原本單檔 server.py 拆出，集中管理 REST 端點；需保護的端點掛上 require_auth 依賴。
WebSocket 與應用組裝分別在 ws.py / server.py。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from . import (
    auth,
    backlog,
    blueprint,
    config,
    history,
    projects,
    publisher,
    redeploy,
    repo_base,
    settings,
    workspace,
    ws,
)

router = APIRouter()

# 敏感寫入路由統一掛此依賴組，避免未來新增路由漏掛。
# require_admin：門禁啟用 → 僅登入門禁（外網登入後可用，未登入 401）；
# 門禁停用 → fail-safe 退回僅限本機（403），不把控制面裸露給全網。
WRITE_DEPS = [Depends(auth.require_admin)]


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


# --- 運維可視化（受保護）----------------------------------------------
@router.get("/api/metrics", dependencies=[Depends(auth.require_auth)])
async def metrics() -> JSONResponse:
    """運維指標：活躍場次 / 並發上限、history 各狀態數與保留策略、workspace 目錄數、並行統計。"""
    sessions = history.list_sessions()
    by_status: dict[str, int] = {}
    for m in sessions:
        s = m.get("status", "unknown")
        by_status[s] = by_status.get(s, 0) + 1
    return JSONResponse(
        {
            "sessions": {
                "active": ws.active_session_count(),
                "max_concurrent": config.MAX_CONCURRENT_SESSIONS,
            },
            "history": {
                "total": len(sessions),
                "by_status": by_status,
                "retention": {
                    "max_count": config.HISTORY_MAX_COUNT,
                    "max_age_s": config.HISTORY_MAX_AGE,
                },
            },
            "workspaces": {"count": workspace.count_workspaces()},
            "parallel": _aggregate_parallel(sessions),
            "scorecard": _aggregate_scorecard(sessions),
        }
    )


def _aggregate_scorecard(sessions: list[dict]) -> dict:
    """跨 session 聚合成果記分卡：成功率、平均輪數、一次過率、退回原因，與近期趨勢。

    趨勢取「最近 10 場 vs 再前 10 場」（sessions 已新→舊排序）——這是『工作室有沒有
    越做越進步』的直接量測：成功率升、平均輪數降＝在進步。
    """
    rows = [
        (m, m["scorecard"])
        for m in sessions
        if m.get("status") != "running" and isinstance(m.get("scorecard"), dict)
    ]
    if not rows:
        return {"n": 0}

    def _slice_stats(part: list[tuple[dict, dict]]) -> dict:
        if not part:
            return {"n": 0}
        done = sum(1 for m, _ in part if m.get("status") == "completed")
        rounds = [s["avg_rounds"] for _, s in part if s.get("avg_rounds")]
        return {
            "n": len(part),
            "completed_rate": round(done / len(part), 2),
            "avg_rounds": round(sum(rounds) / len(rounds), 2) if rounds else None,
        }

    rejects = {"qa_fail": 0, "smoke_fail": 0, "gate_veto": 0, "critic": 0, "stall": 0}
    tasks_total = tasks_done = first_try = 0
    for _, s in rows:
        for k in rejects:
            rejects[k] += (s.get("rejects") or {}).get(k, 0)
        tasks_total += s.get("tasks_total", 0)
        tasks_done += s.get("tasks_done", 0)
        first_try += s.get("first_try_done", 0)
    return {
        **_slice_stats(rows),
        "tasks": {
            "total": tasks_total,
            "done": tasks_done,
            "first_try_done": first_try,
            "first_try_rate": round(first_try / tasks_done, 2) if tasks_done else None,
        },
        "rejects": rejects,
        "trend": {"recent": _slice_stats(rows[:10]), "previous": _slice_stats(rows[10:20])},
    }


def _aggregate_parallel(sessions: list[dict]) -> dict:
    """跨 session 聚合並行可觀測性：曾並行的場次數、峰值支線、合併衝突、平均加速比與省下的時間。"""
    runs = [
        p for m in sessions if (p := m.get("parallel")) and isinstance(p, dict) and p.get("enabled")
    ]
    if not runs:
        return {
            "enabled_runs": 0,
            "config": {
                "enabled": config.PARALLEL_TASKS_ENABLED,
                "lanes": config.PARALLEL_LANES,
            },
        }
    speedups = [r.get("speedup", 1.0) for r in runs]
    saved = sum(r.get("serial_estimate_s", 0) - r.get("wall_clock_s", 0) for r in runs)
    return {
        "enabled_runs": len(runs),
        "peak_lanes": max(r.get("lanes_max", 0) for r in runs),
        "total_waves": sum(r.get("waves", 0) for r in runs),
        "merge_conflicts": sum(r.get("merge_conflicts", 0) for r in runs),
        # 降級可觀測性：並行實際退回主幹序列化的頻率（lane 崩潰 / 無法隔離 / 合併衝突重跑）。
        "lane_exceptions": sum(r.get("lane_exceptions", 0) for r in runs),
        "deferred": sum(r.get("deferred", 0) for r in runs),
        "conflict_retries": sum(r.get("conflict_retries", 0) for r in runs),
        "lane_resolved": sum(r.get("lane_resolved", 0) for r in runs),
        "avg_speedup": round(sum(speedups) / len(speedups), 2),
        "wall_clock_saved_s": round(saved, 1),
        "config": {
            "enabled": config.PARALLEL_TASKS_ENABLED,
            "lanes": config.PARALLEL_LANES,
        },
    }


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

    - 門禁已啟用：require_admin 走登入門禁確保已登入，再驗證『目前密碼』正確才放行。
    - 門禁未啟用：受 fail-safe 限本機，首次設定密碼須由本機執行（此時無需目前密碼）。
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


@router.post("/api/history/cleanup/retention", dependencies=[Depends(auth.require_auth)])
async def history_cleanup_retention() -> JSONResponse:
    """依保留策略（TI_HISTORY_MAX_COUNT / TI_HISTORY_MAX_AGE）手動觸發一次回收。"""
    return JSONResponse({"deleted": history.enforce_retention()})


# --- 專案（長期產品；受保護）--------------------------------------------
class ProjectBody(BaseModel):
    name: str
    vision: str = ""


class ProjectTaskBody(BaseModel):
    title: str
    detail: str = ""
    priority: int = 1  # P0 必須 ~ P2 加分（越小越優先；越界由 backlog 夾值）
    type: str = "improvement"  # feature | bug | improvement


@router.get("/api/projects", dependencies=[Depends(auth.require_auth)])
async def projects_list() -> JSONResponse:
    """所有專案＋各自 backlog 統計（前端專案選單/面板用）。"""
    out = []
    for meta in projects.list_projects():
        out.append(
            {
                **meta,
                "backlog": backlog.counts(state_dir=projects.state_dir(meta["id"])),
                "workspace_id": projects.workspace_id(meta["id"]),
            }
        )
    return JSONResponse({"projects": out})


@router.post("/api/projects", dependencies=[Depends(auth.require_auth)])
async def projects_create(body: ProjectBody) -> JSONResponse:
    meta = projects.create(body.name, body.vision)
    if meta is None:
        return JSONResponse({"error": "名稱不可為空"}, status_code=400)
    return JSONResponse({"project": meta})


@router.get("/api/projects/{project_id}", dependencies=[Depends(auth.require_auth)])
async def projects_detail(project_id: str) -> JSONResponse:
    meta = projects.get(project_id)
    if meta is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    sdir = projects.state_dir(project_id)
    # backlog 按消化順序回傳（priority 小者先、同級內先進先出），前端不必自己排。
    tasks = sorted(
        backlog.list_tasks(state_dir=sdir),
        key=lambda t: (t.get("priority", 1), t.get("created_at", 0)),
    )
    return JSONResponse(
        {
            "project": meta,
            "workspace_id": projects.workspace_id(project_id),
            "backlog": tasks,
            "counts": backlog.counts(state_dir=sdir),
            "blueprint": blueprint.load(project_id),
        }
    )


@router.post("/api/projects/{project_id}/backlog", dependencies=[Depends(auth.require_auth)])
async def projects_add_task(project_id: str, body: ProjectTaskBody) -> JSONResponse:
    """手動往專案 backlog 排一個改良任務（持續改良迴圈會撿走）。"""
    if projects.get(project_id) is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    task = backlog.add(
        body.title,
        body.detail,
        source="user",
        state_dir=projects.state_dir(project_id),
        priority=body.priority,
        item_type=body.type,
    )
    if task is None:
        return JSONResponse({"error": "標題不可為空或與待辦重複"}, status_code=400)
    return JSONResponse({"task": task})


class PublishRepoBody(BaseModel):
    repo: str = ""


@router.post("/api/projects/{project_id}/publish-repo", dependencies=[Depends(auth.require_auth)])
async def projects_set_publish_repo(project_id: str, body: PublishRepoBody) -> JSONResponse:
    """設定專案的目標 repo（owner/repo；留空＝清除）＝工作基底＋發佈目標。

    workspace 全新時，下一場 session 開始前會 clone 該 repo 當工作基底（專家在使用者
    指定的程式碼上修改）；已同源則每場快轉到遠端 base；成果推分支並對 base 開 PR
    （repo 不存在且 owner 為 token 使用者時自動建私有 repo；空 repo 首次發佈初始化 base）。

    這裡刻意「不」在設定當下 clone：此時 GITHUB_TOKEN 可能尚未設定、大 repo 會拖垮
    HTTP 請求；clone 只是同步狀態機的一個分支，統一延後到 session 開始（repo_base）。
    僅做唯讀檢查：workspace 已有獨立內容時回 warning（絕不清空既有內容）。
    """
    if projects.get(project_id) is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    meta = projects.set_publish_repo(project_id, body.repo)
    if meta is None:
        return JSONResponse({"error": "格式須為 owner/repo（或留空清除）"}, status_code=400)
    state = await repo_base.workspace_state(projects.workspace_dir(project_id))
    warning = None
    if (body.repo or "").strip() and state in ("has_history", "local_files"):
        warning = (
            "此專案 workspace 已有獨立內容／歷史，無法改以該 repo 為工作基底（既有內容絕不清空）。"
            "若這份歷史本就源自該 repo，session 開始時會自動快轉同步；"
            "否則成果仍會推分支保存，但 PR 會因無共同歷史而開不成"
        )
    return JSONResponse({"project": meta, "base_state": state, "warning": warning})


@router.post("/api/projects/{project_id}/recover", dependencies=[Depends(auth.require_auth)])
async def projects_recover(project_id: str) -> JSONResponse:
    """中斷恢復：服務重啟／行程被殺後，把殘留狀態清乾淨，讓改良迴圈可以無痛重啟。

    做兩件事（皆冪等）：
    1. 卡在 in_progress 的 backlog 任務重置回 pending（中斷殘留不是真失敗，failed 不動）。
    2. 這些任務對應的幽靈 session meta（永遠停在 running）標為 error，歷史列表不再誤顯。
    改良迴圈正在跑時拒絕（409）——in_progress 是進行中的正常狀態，不能搶著重置。
    前端收到 ok 後負責以既有 improve 流程重啟迴圈（事件照常即時串流）。
    """
    if projects.get(project_id) is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    if project_id in ws._active_projects:
        return JSONResponse({"error": "改良迴圈正在進行中，無需恢復"}, status_code=409)
    sdir = projects.state_dir(project_id)
    reset = 0
    for t in backlog.list_tasks("in_progress", state_dir=sdir):
        sid = (t.get("session_id") or "").strip()
        if sid:
            history.mark_interrupted(sid, "中斷恢復：服務重啟或行程中斷，任務已重置回待辦")
        backlog.set_status(t["id"], "pending", state_dir=sdir, note="中斷恢復：重置重跑")
        reset += 1
    return JSONResponse({"ok": True, "reset": reset, "counts": backlog.counts(state_dir=sdir)})


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
