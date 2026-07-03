"""HTTP API 路由（health、登入/登出、workspace、history、publish）。

從原本單檔 server.py 拆出，集中管理 REST 端點；需保護的端點掛上 require_auth 依賴。
WebSocket 與應用組裝分別在 ws.py / server.py。
"""

from __future__ import annotations

import asyncio
import json
import subprocess

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from . import (
    auth,
    backlog,
    blueprint,
    claude_accounts,
    config,
    history,
    projects,
    provider_quota,
    publisher,
    redeploy,
    repo_base,
    role_store,
    roles,
    settings,
    workflow,
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
        return {
            "n": 0,
            "qa_pass_rate": None,
            "critic_pass_rate": None,
            "demo_pass_rate": None,
        }

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
    qa_total = qa_pass = 0
    critic_total = critic_pass = 0
    demo_total = demo_pass = 0
    for _, s in rows:
        for k in rejects:
            rejects[k] += (s.get("rejects") or {}).get(k, 0)
        tasks_total += s.get("tasks_total", 0)
        tasks_done += s.get("tasks_done", 0)
        first_try += s.get("first_try_done", 0)
        qa_total += s.get("qa_total", 0)
        qa_pass += s.get("qa_pass", 0)
        critic_total += s.get("critic_total", 0)
        critic_pass += s.get("critic_pass", 0)
        demo = s.get("demo_passed")
        if demo is not None:
            demo_total += 1
            if demo is True:
                demo_pass += 1

    def _rate(passed: int, total: int) -> float | None:
        return round(passed / total, 2) if total else None

    return {
        **_slice_stats(rows),
        "qa_pass_rate": _rate(qa_pass, qa_total),
        "critic_pass_rate": _rate(critic_pass, critic_total),
        "demo_pass_rate": _rate(demo_pass, demo_total),
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
async def login(request: Request, body: LoginBody) -> JSONResponse:
    if not config.auth_enabled():
        return JSONResponse({"ok": True, "detail": "門禁未啟用"})
    client = request.client.host if request.client else "?"
    # 速率限制：連續失敗達上限即鎖定，期間直接拒絕（不比對密碼），擋暴力破解。
    wait = auth.login_lock_remaining(client)
    if wait > 0:
        return JSONResponse(
            {"ok": False, "detail": f"嘗試過多，請 {int(wait) + 1} 秒後再試"},
            status_code=429,
        )
    ok = auth.check_password(body.password)
    auth.register_login_result(client, ok)
    if not ok:
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


@router.get("/api/provider-quota", dependencies=[Depends(auth.require_auth)])
async def get_provider_quota() -> JSONResponse:
    """Provider 額度/狀態總覽。

    回傳內容刻意只含非秘密資訊：登入/ready 狀態、可列出的模型、Ti 本機累積 token 用量。
    官方 subscription quota 若 provider CLI 未提供穩定非互動 API，回傳狀態說明而不讀取/暴露憑證。
    聚合邏輯已抽到 studio/provider_quota.py（供 orchestrator 動態分派共用，避免反向 import）。
    """
    # snapshot 內含對外阻塞 HTTP 查詢；丟到 thread 避免卡住事件迴圈（其他請求照常）。
    return JSONResponse(await asyncio.to_thread(provider_quota.snapshot))


# 向後相容別名：tests/settings/test_provider_quota.py 等仍以 routes._provider_quota_snapshot 取用。
_provider_quota_snapshot = provider_quota.snapshot


# --- Claude 多帳號切換（受保護）----------------------------------------
class ClaudeAccountSwitch(BaseModel):
    """POST /api/claude-account/switch 的請求本體。"""

    label: str


def _schedule_service_restart() -> None:
    """1 秒後重啟 ti.service + ti-autopilot，讓換檔後的新 Claude 認證生效。

    用 systemd-run 起一次性 transient timer：它脫離 ti.service 的 cgroup，故 restart 殺掉
    本服務時不會把「重啟動作本身」一起殺掉；1 秒延遲確保切換端點的 200 回應已送達前端。
    無 systemd-run（權限/環境）時退回 detached subprocess，盡力而為。
    """
    cmd = ["systemctl", "restart", "ti.service", "ti-autopilot"]
    try:
        subprocess.Popen(
            ["systemd-run", "--no-block", "--on-active=1", "--", *cmd],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return
    except OSError:
        pass
    subprocess.Popen(
        ["bash", "-c", "sleep 1; " + " ".join(cmd)],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


@router.post("/api/claude-account/switch", dependencies=WRITE_DEPS)
async def claude_account_switch(body: ClaudeAccountSwitch) -> JSONResponse:
    """切換 Claude 在線訂閱帳號（換憑證檔 + 重啟服務使新認證生效）。

    認證在 SDK 啟動時載入記憶體，換檔後須重啟 ti.service/ti-autopilot 才生效；重啟會中斷
    互動討論與 autopilot 任務，故先擋下「進行中」狀態（回 409），閒置才放行。
    """
    busy: list[str] = []
    if ws.active_session_count() > 0:
        busy.append("有互動討論正在進行")
    in_prog = backlog.list_tasks("in_progress")
    if in_prog:
        busy.append(f"autopilot 有 {len(in_prog)} 個任務進行中")
    if busy:
        return JSONResponse({"ok": False, "error": "busy", "reasons": busy}, status_code=409)

    try:
        claude_accounts.switch(body.label)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

    _schedule_service_restart()
    return JSONResponse({"ok": True, "label": body.label, "restarting": True})


# --- 角色管理（受保護）--------------------------------------------------
class RoleBody(BaseModel):
    """POST/PUT /api/roles 的請求本體。

    ``system_prompt`` 為「角色專屬段」（不含共通守則 _COMMON，載入時自動前置）；
    須非空且至少一行含「輸出/決議/驗證/格式/指令/決策」緊接冒號（反空殼 persona）。
    PUT 為整筆替換語意：未給的選填欄位回到預設值。
    """

    key: str = ""  # POST 必填；PUT 可省略（給了須與路徑一致）
    name: str
    system_prompt: str
    avatar: str = "🤖"
    title: str = ""
    model: str = ""  # 空字串 → config.MODEL_FAST
    allowed_tools: list[str] = Field(default_factory=lambda: ["Read", "Grep"])
    permission_mode: str = "default"  # 白名單 {default, acceptEdits}
    tags: list[str] = Field(default_factory=list)
    description: str = ""


def _role_json(role: roles.Role) -> dict:
    """單一角色的 API 回傳形狀：Role 欄位＋來源標記＋「角色專屬 body 原文」。

    system_prompt 回去除 _COMMON 前綴的原文——讓「GET 讀出→改→PUT 寫回」直接往返。
    """
    return {
        "key": role.key,
        "name": role.name,
        "avatar": role.avatar,
        "title": role.title,
        "model": role.model,
        "allowed_tools": list(role.allowed_tools),
        "permission_mode": role.permission_mode,
        "tags": list(role.tags),
        "description": role.description,
        "source": role_store.role_source(role.key),
        "in_roster": any(r.key == role.key for r in roles.ROSTER),
        "system_prompt": role_store.builtin_body(role).strip(),
    }


def _bad_key_response(key: str) -> JSONResponse:
    return JSONResponse(
        {"ok": False, "detail": f"key {key!r} 不合法（須符合 {role_store.KEY_RE.pattern}）"},
        status_code=422,
    )


@router.get("/api/roles", dependencies=[Depends(auth.require_auth)])
async def roles_list() -> JSONResponse:
    """全部角色（內建＋檔案；含被 OPTIONAL_ROLES 過濾出 ROSTER 者，以 in_roster 區分）。"""
    return JSONResponse({"roles": [_role_json(r) for r in roles.BY_KEY.values()]})


@router.post("/api/roles", dependencies=WRITE_DEPS)
async def roles_create(body: RoleBody) -> JSONResponse:
    """建立角色：落檔 roles/<key>.md 並 reload。內建 key ＝建立覆蓋檔（允許）；
    已有角色檔的 key 回 409（請用 PUT 編輯）。"""
    key = body.key.strip()
    if not role_store.KEY_RE.match(key):
        return _bad_key_response(key)
    if role_store.role_source(key) in ("override", "file"):
        return JSONResponse(
            {"ok": False, "detail": f"角色 {key!r} 已存在，請用 PUT /api/roles/{key} 編輯"},
            status_code=409,
        )
    try:
        role = role_store.save_role(
            key, body.model_dump(exclude={"key", "system_prompt"}), body.system_prompt
        )
    except role_store.RoleFileError as e:
        return JSONResponse({"ok": False, "detail": str(e)}, status_code=422)
    return JSONResponse({"ok": True, "role": _role_json(role)})


@router.put("/api/roles/{key}", dependencies=WRITE_DEPS)
async def roles_update(key: str, body: RoleBody) -> JSONResponse:
    """編輯角色（整筆替換）：對內建角色＝寫覆蓋檔；對檔案角色＝改寫原檔。"""
    if not role_store.KEY_RE.match(key):
        return _bad_key_response(key)
    if body.key and body.key.strip() != key:
        return JSONResponse(
            {"ok": False, "detail": f"body key={body.key!r} 與路徑 {key!r} 不一致"},
            status_code=422,
        )
    if key not in roles.BY_KEY:
        return JSONResponse({"ok": False, "detail": f"角色 {key!r} 不存在"}, status_code=404)
    try:
        role = role_store.save_role(
            key, body.model_dump(exclude={"key", "system_prompt"}), body.system_prompt
        )
    except role_store.RoleFileError as e:
        return JSONResponse({"ok": False, "detail": str(e)}, status_code=422)
    return JSONResponse({"ok": True, "role": _role_json(role)})


@router.delete("/api/roles/{key}", dependencies=WRITE_DEPS)
async def roles_delete(key: str) -> JSONResponse:
    """刪除角色檔：file＝移除自建角色；override＝還原內建；純內建回 409、不存在回 404。"""
    if not role_store.KEY_RE.match(key):
        return _bad_key_response(key)
    source = role_store.role_source(key)
    if source == "builtin":
        return JSONResponse(
            {"ok": False, "detail": f"內建角色 {key!r} 不可刪除（刪除其覆蓋檔即還原內建）"},
            status_code=409,
        )
    if source == "unknown":
        return JSONResponse({"ok": False, "detail": f"角色 {key!r} 不存在"}, status_code=404)
    role_store.delete_role_file(key)
    return JSONResponse({"ok": True, "restored_builtin": source == "override"})


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
            # 進行中與否（前端據此顯示「停止執行」、預期刪除會被 409 擋下）
            "active": project_id in ws._active_projects,
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


@router.delete("/api/projects/{project_id}", dependencies=[Depends(auth.require_auth)])
async def projects_delete(project_id: str) -> JSONResponse:
    """刪除專案：meta／改良待辦／藍圖與固定 workspace（含 .lanes 兜底）。

    進行中（改良迴圈或單場討論佔用 workspace）回 409——先停止再刪，避免專家
    對著被抽掉的目錄繼續寫檔。history 的 session 紀錄保留（仍可重播），可從
    歷史面板各自刪除。
    """
    if projects.get(project_id) is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    if project_id in ws._active_projects:
        return JSONResponse({"error": "專案有進行中的討論，請先停止執行再刪除"}, status_code=409)
    ok = projects.delete(project_id)
    return JSONResponse({"ok": ok}, status_code=200 if ok else 404)


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


# --- 進行中討論的停止（受保護）------------------------------------------
@router.post("/api/sessions/{target_id}/stop", dependencies=[Depends(auth.require_auth)])
async def session_stop(target_id: str) -> JSONResponse:
    """對進行中的討論／持續改良迴圈送停止指令；target 可為 session id 或專案 id。

    與 WS 的 stop 同一條 request_stop 管線——原 WS 連線斷開（頁面重整／detach
    背景續跑）後仍能喊停。停止是「請求」非立即中斷：編排在安全點收尾、照常發
    DONE 與寫 history。找不到進行中的目標回 404。
    """
    ok = ws.stop_running(target_id)
    return JSONResponse({"ok": ok}, status_code=200 if ok else 404)


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


@router.post("/api/publish/{session_id}", dependencies=WRITE_DEPS)
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


# --- 討論小組（受保護）--------------------------------------------------
class GroupBody(BaseModel):
    """POST /api/groups 的請求體。mode 白名單 {round_robin, parallel}。"""

    name: str
    role_keys: list[str]
    mode: str = "round_robin"


class GroupUpdateBody(BaseModel):
    """PUT /api/groups/{name} 的請求體（name 由路徑決定、不可改名）。

    整筆替換語意，故 role_keys 與 mode 皆必填——防止漏帶 mode 被預設值默默重置。
    """

    role_keys: list[str]
    mode: str


@router.get("/api/groups", dependencies=[Depends(auth.require_auth)])
async def groups_list() -> JSONResponse:
    """全部討論小組（[{name, role_keys, mode}]）。groups.yaml 損壞回 500 並附原因。"""
    try:
        return JSONResponse({"groups": role_store.list_groups()})
    except role_store.GroupFileError as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/groups", dependencies=WRITE_DEPS)
async def groups_create(body: GroupBody) -> JSONResponse:
    """建立小組。驗證失敗（key 不存在/重複/<2 人/非法 mode）回 422；同名已存在回 409。"""
    try:
        group = role_store.create_group(body.name, body.role_keys, body.mode)
    except role_store.GroupError as e:
        return JSONResponse({"error": str(e)}, status_code=422)
    except role_store.GroupFileError as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    if group is None:
        return JSONResponse({"error": f"小組 {body.name.strip()!r} 已存在"}, status_code=409)
    return JSONResponse({"group": group})


@router.put("/api/groups/{name}", dependencies=WRITE_DEPS)
async def groups_update(name: str, body: GroupUpdateBody) -> JSONResponse:
    """整筆更新小組（role_keys＋mode）。驗證失敗回 422；小組不存在回 404。"""
    try:
        group = role_store.update_group(name, body.role_keys, body.mode)
    except role_store.GroupError as e:
        return JSONResponse({"error": str(e)}, status_code=422)
    except role_store.GroupFileError as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    if group is None:
        return JSONResponse({"error": f"小組 {name!r} 不存在"}, status_code=404)
    return JSONResponse({"group": group})


@router.delete("/api/groups/{name}", dependencies=WRITE_DEPS)
async def groups_delete(name: str) -> JSONResponse:
    """刪除小組；不存在回 404。"""
    try:
        ok = role_store.delete_group(name)
    except role_store.GroupFileError as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    return JSONResponse({"ok": ok}, status_code=200 if ok else 404)


# --- 動態流程 workflow（受保護）----------------------------------------
class WorkflowBody(BaseModel):
    """POST /api/workflows 的請求體。stages 為 stage dict 列表，由 workflow 層硬驗證。"""

    name: str
    description: str = ""
    stages: list[dict]


class WorkflowUpdateBody(BaseModel):
    """PUT /api/workflows/{name} 的請求體（name 由路徑決定、不可改名）。整筆替換語意。"""

    description: str = ""
    stages: list[dict]


@router.get("/api/workflows", dependencies=[Depends(auth.require_auth)])
async def workflows_list() -> JSONResponse:
    """全部動態流程（[{name, description, stages}]）＋內建保留流程。workflows.yaml 損壞回 500。"""
    try:
        items = workflow.list_workflows()
    except workflow.WorkflowFileError as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    # 內建保留流程（預設流程／動態優先）永遠可選（不存檔）——依保留順序附在最前供 UI 一律可選。
    builtins = [workflow.get_workflow(n) for n in workflow.RESERVED_NAMES]
    items = [b for b in builtins if b] + [
        w for w in items if w["name"] not in workflow.RESERVED_NAMES
    ]
    return JSONResponse({"workflows": items})


@router.post("/api/workflows", dependencies=WRITE_DEPS)
async def workflows_create(body: WorkflowBody) -> JSONResponse:
    """建立動態流程。驗證失敗（型別/角色/verdict/結構）回 422；同名（含保留預設名）回 409。"""
    try:
        wf = workflow.create_workflow(body.name, body.description, body.stages)
    except workflow.WorkflowError as e:
        return JSONResponse({"error": str(e)}, status_code=422)
    except workflow.WorkflowFileError as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    if wf is None:
        return JSONResponse({"error": f"流程 {body.name.strip()!r} 已存在"}, status_code=409)
    return JSONResponse({"workflow": wf})


@router.put("/api/workflows/{name}", dependencies=WRITE_DEPS)
async def workflows_update(name: str, body: WorkflowUpdateBody) -> JSONResponse:
    """整筆更新動態流程（description＋stages）。驗證失敗回 422；不存在回 404。"""
    try:
        wf = workflow.update_workflow(name, body.description, body.stages)
    except workflow.WorkflowError as e:
        return JSONResponse({"error": str(e)}, status_code=422)
    except workflow.WorkflowFileError as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    if wf is None:
        return JSONResponse({"error": f"流程 {name!r} 不存在"}, status_code=404)
    return JSONResponse({"workflow": wf})


@router.delete("/api/workflows/{name}", dependencies=WRITE_DEPS)
async def workflows_delete(name: str) -> JSONResponse:
    """刪除動態流程；不存在回 404。"""
    try:
        ok = workflow.delete_workflow(name)
    except workflow.WorkflowFileError as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    return JSONResponse({"ok": ok}, status_code=200 if ok else 404)


# --- autopilot（受保護）------------------------------------------------
class TaskBody(BaseModel):
    title: str = ""
    detail: str = ""


@router.get("/api/autopilot", dependencies=[Depends(auth.require_auth)])
async def autopilot_status() -> JSONResponse:
    # 心跳：autopilot 主迴圈每輪寫入的 status.json（state=idle/running/quota_sleep、
    # task_id、sleep_until、各 provider 用量）。檔案不存在或壞損＝null（尚未跑過/未寫入）。
    try:
        heartbeat = json.loads(
            (config.AUTOPILOT_STATE_DIR / "status.json").read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        heartbeat = None
    return JSONResponse(
        {
            "paused": config.autopilot_paused(),
            "counts": backlog.counts(),
            "dryrun": config.AUTOPILOT_DRYRUN,
            "repo": config.AUTOPILOT_REPO,
            "heartbeat": heartbeat,
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
