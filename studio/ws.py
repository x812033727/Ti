"""WebSocket 端點：即時串流專家討論，並接收人類插話 / 停止指令。

從原本單檔 server.py 拆出。門禁啟用時，握手後會先檢查登入 cookie。
"""

from __future__ import annotations

import asyncio
import uuid

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from . import auth, backlog, config, events, history, projects, runner, workspace
from .events import StudioEvent
from .improver import ProjectImprover
from .orchestrator import StudioSession

router = APIRouter()

# 客戶端斷線後仍在背景跑完的討論任務（持有參考避免被 GC 回收）。
_detached: set[asyncio.Task] = set()

# 進行中的專案 id：同一專案共用固定 workspace，同時兩場討論會互相踩檔案，故擋第二場。
_active_projects: set[str] = set()

# 同時進行中的討論場次數（並發上限用）。每場占一個 slot，隨 run_task 完成釋放
# （含客戶端斷線後背景續跑）。單執行緒 event loop 內，slot 的 check 與增減之間無
# await，故為原子操作、不需鎖。
_active_sessions = 0


def _acquire_session_slot() -> bool:
    """嘗試占用一個並發 slot；達 config.MAX_CONCURRENT_SESSIONS 上限回 False。0 = 不限。"""
    global _active_sessions
    limit = config.MAX_CONCURRENT_SESSIONS
    if limit > 0 and _active_sessions >= limit:
        return False
    _active_sessions += 1
    return True


def _release_session_slot() -> None:
    """釋放一個並發 slot（夾在 0 以上，防重複釋放造成負數）。"""
    global _active_sessions
    if _active_sessions > 0:
        _active_sessions -= 1


def active_session_count() -> int:
    """目前進行中的討論場次數（供運維可視化 /api/metrics）。"""
    return _active_sessions


@router.websocket("/ws")
async def ws(websocket: WebSocket) -> None:
    await websocket.accept()

    # /ws 是核心產品入口（啟動多專家討論）。刻意「不」限定本機來源：對外網站須能讓
    # 已登入使用者開討論，否則整個對外服務形同癱瘓。安全模型改為「登入門禁（共用密碼）
    # + 專家 bash 一律 bwrap 沙箱（host 唯讀、PID/網路隔離）」。HTTP 管理類寫入
    # （settings / redeploy / autopilot）同此模型：門禁啟用時登入即可（require_admin），
    # 門禁停用時 fail-safe 退回僅限本機。
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
    slot_held = False
    project_held = False
    run_task: asyncio.Task | None = None

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
        # 第一則訊息為產品需求（可選擇附帶要 clone 的 GitHub repo，或指定長期專案）。
        # project_id：在該專案的固定 workspace 上工作（程式碼跨場次累積）。
        # mode="improve"：啟動持續改良迴圈（需搭配 project_id；requirement 選填＝先排進 backlog）。
        data = await websocket.receive_json()
        requirement = (data.get("requirement") or "").strip()
        repo_url = (data.get("repo_url") or "").strip()
        repo_branch = (data.get("repo_branch") or "").strip() or None
        project_id = (data.get("project_id") or "").strip()
        improve_mode = (data.get("mode") or "").strip() == "improve"

        project = projects.get(project_id) if project_id else None
        if project_id and project is None:
            await websocket.send_json({"type": "error", "payload": {"message": "找不到該專案"}})
            await websocket.close()
            return
        if improve_mode and project is None:
            await websocket.send_json(
                {"type": "error", "payload": {"message": "持續改良需先選擇一個專案"}}
            )
            await websocket.close()
            return
        if project is not None and repo_url:
            await websocket.send_json(
                {
                    "type": "error",
                    "payload": {
                        "message": "專案模式不支援同時指定 repo 網址（專案已有固定 workspace）"
                    },
                }
            )
            await websocket.close()
            return
        # 需求必填；唯「專案 + 持續改良」可留空（由 backlog／找問題供給任務）。
        if not requirement and not (improve_mode and project is not None):
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

        # 並發上限：避免大量同時連線各自起一堆專家子程序 / LLM 連線而耗盡資源/額度。
        # slot 隨 run_task 完成釋放（含斷線後背景續跑）；0 = 不限。
        if not _acquire_session_slot():
            await websocket.send_json(
                {
                    "type": "error",
                    "payload": {
                        "message": (
                            f"目前同時進行的討論已達上限（{config.MAX_CONCURRENT_SESSIONS}），"
                            "請稍後再試"
                        )
                    },
                }
            )
            await websocket.close(code=1013)  # 1013 = Try Again Later
            return
        slot_held = True

        # 專案互斥：固定 workspace 不能同時兩場討論互踩，同一專案僅允許一場。
        if project is not None:
            if project["id"] in _active_projects:
                await websocket.send_json(
                    {"type": "error", "payload": {"message": "該專案已有進行中的討論，請稍後再試"}}
                )
                await websocket.close(code=1013)
                return
            _active_projects.add(project["id"])
            project_held = True

        queue: asyncio.Queue[str] = asyncio.Queue()

        if improve_mode:
            # 持續改良迴圈：requirement 有值就先排進專案 backlog（使用者指定的改良方向）。
            # 各輪討論由 improver 自行記錄成獨立 history session，外層不開錄。
            sdir = projects.state_dir(project["id"])
            if requirement:
                backlog.add(requirement, source="user", state_dir=sdir)
            improver = ProjectImprover(project, broadcast, intervention_queue=queue)
            run_task = asyncio.create_task(improver.run())
            run_task.add_done_callback(lambda _t: _release_session_slot())
            run_task.add_done_callback(lambda _t: _active_projects.discard(project["id"]))
            await _pump_interventions(websocket, improver, queue, run_task)
            if not run_task.done():
                _detached.add(run_task)
                run_task.add_done_callback(_detached.discard)
            return

        if project is not None:
            # 專案固定 workspace：絕不清空，程式碼與 git 歷史跨場次累積。
            cwd = projects.workspace_dir(project["id"])
        else:
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

        label = f"[專案 {project['name']}] {requirement}" if project is not None else requirement
        history.start_session(session_id, label)
        recording = True
        experts = None
        critics = None
        if config.OFFLINE_MODE:
            from .fake_experts import build_fake_critics, build_fake_experts

            experts = build_fake_experts(session_id, cwd, requirement)
            # 注入離線 critic，讓 demo 端到端展示一次「內部討論」（critic_review）事件。
            critics = build_fake_critics(session_id, cwd)
        session = StudioSession(
            session_id,
            broadcast,
            experts=experts,
            cwd=cwd,
            intervention_queue=queue,
            repo_url=repo_url or None,
            critics=critics,
            workspace_id=projects.workspace_id(project["id"]) if project is not None else None,
        )
        if config.OFFLINE_MODE:
            # 離線並行 demo：每條 lane 用假專家工廠（各自寫該任務的檔），無金鑰也能跑多支線。
            from .fake_experts import build_fake_lane_expert

            session._lane_expert_factory = build_fake_lane_expert

        # 編排在背景跑，主迴圈同時接收人類插話 / 停止指令。
        # 任務生命週期與這條連線解耦：用 done callback 負責收尾（finish_session），
        # 即使客戶端中途斷線，討論仍能在背景跑到完成，事件照寫 history。
        run_coro = (
            _run_project_session(session, requirement, project)
            if project is not None
            else session.run(requirement)
        )
        run_task = asyncio.create_task(run_coro)
        # slot 隨 run_task 完成釋放（無論是否 detach、是否斷線），一次性、不重複。
        run_task.add_done_callback(lambda _t: _release_session_slot())
        if project is not None:
            pid = project["id"]
            run_task.add_done_callback(lambda _t: _active_projects.discard(pid))
        await _pump_interventions(websocket, session, queue, run_task)
        if not run_task.done():
            # 客戶端已斷線（或按 stop 後尚未結束）：把討論留在背景跑完，handler 立即
            # 返回，不阻塞 uvicorn 關閉、也不把斷線當成停止。收尾交給 callback。
            _detached.add(run_task)

            def _finish(task: asyncio.Task) -> None:
                if recording:
                    history.finish_session(session_id)
                _detached.discard(task)

            run_task.add_done_callback(_finish)
    except WebSocketDisconnect:
        pass
    finally:
        # slot：已建 run_task 者由其 done-callback 釋放；若占用後在建 task 前就 return
        # （驗證失敗 / 例外），在此補釋放，避免 slot 永久洩漏。
        if slot_held and run_task is None:
            _release_session_slot()
        # 專案互斥同理：已建 run_task 者由其 done-callback 釋放；建 task 前出錯在此補釋放。
        if project_held and run_task is None:
            _active_projects.discard(project["id"])
        # 已 detach（背景跑、尚未結束）的任務由 callback 收尾；其餘（正常跑完、或在
        # 建立任務前就出錯）在此同步收尾，確保歷史狀態即時更新、不被重複呼叫。
        if recording and (run_task is None or run_task.done()):
            history.finish_session(session_id)
        try:
            await websocket.close()
        except RuntimeError:
            pass


async def _run_project_session(session: StudioSession, requirement: str, project: dict) -> dict:
    """專案內的單場討論：跑完後把檢討發現的後續任務回填專案 backlog、足跡記到專案 meta。

    這條回填線讓「手動單場討論」也參與持續改良——下次開持續改良迴圈時，
    這些後續任務就是現成的供給。
    """
    result = await session.run(requirement)
    sdir = projects.state_dir(project["id"])
    followups = result.get("followups") or []
    if followups:
        backlog.add_many(followups, source="discovered", state_dir=sdir)
    projects.record_session(
        project["id"], session.session_id, requirement[:80], bool(result.get("completed"))
    )
    return result


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
                # 收到即回顯（寫 history + 推前端），使用者立刻看到插話已送達；專家於下一次
                # drain 納入。否則並行模式要等到波次邊界才 broadcast，期間畫面毫無反應＝「沒用」。
                await session.broadcast(events.human_message(session.session_id, text))
        elif kind == "stop":
            session.request_stop()
            break
