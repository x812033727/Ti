"""WebSocket 端點：即時串流專家討論，並接收人類插話 / 停止指令。

從原本單檔 server.py 拆出。門禁啟用時，握手後會先檢查登入 cookie。
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from . import (
    auth,
    backlog,
    blueprint,
    config,
    events,
    history,
    projects,
    repo_base,
    role_store,
    runner,
    workflow,
    workspace,
)
from .events import StudioEvent
from .improver import ProjectImprover, drain_result_to_backlogs
from .orchestrator import StudioSession

router = APIRouter()

# 客戶端斷線後仍在背景跑完的討論任務（持有參考避免被 GC 回收）。
_detached: set[asyncio.Task] = set()

# 進行中的專案 id：同一專案共用固定 workspace，同時兩場討論會互相踩檔案，故擋第二場。
_active_projects: set[str] = set()

# 進行中討論的控制器（session id／專案 id → StudioSession 或 ProjectImprover）。
# WS 的 stop 只在原連線存活時可用；這張表讓 REST（POST /api/sessions/{id}/stop）
# 在頁面重整／斷線（detach 背景續跑）後仍能對同一條 request_stop 管線喊停。
_running: dict[str, object] = {}


def _register_running(controller: object, *keys: str) -> None:
    for k in keys:
        if k:
            _running[k] = controller


def _unregister_running(*keys: str) -> None:
    for k in keys:
        _running.pop(k, None)


# --- 斷線重掛（attach）：進行中 session 的事件 fan-out -----------------------
#
# 原 broadcast 是綁死單一 socket 的閉包；hub 讓「第二條 WS」能訂閱同一場討論的
# 事件流（先補放 history JSONL 已錯過事件、再無縫接 live），並把 interject/stop
# 餵回既有 controller。單場 StudioSession 走完整快照補放；improve umbrella 無單一
# JSONL（各輪各自入檔），以 live_only hub 註冊——attach 只接續即時事件、不補放。

# 單場 attach 旁聽連線上限（attach 不占 MAX_CONCURRENT_SESSIONS slot——slot 護的是
# 專家子程序/LLM 重資源；若計入，滿載時斷線重連會自我封鎖。此上限防濫用）。
_MAX_ATTACH_LISTENERS = 16


# attach listener 佇列的積壓上限：超過即視為慢消費者踢除（client 帶 cursor 重掛自癒）。
_LISTENER_BACKLOG_MAX = 2048


class _SessionHub:
    """單場討論的事件樞紐。

    不變式：`seq` == history JSONL 已寫入行數——publish 只能緊跟在
    `history.record_event` 之後同步呼叫（兩者間不得插入 await），attach 端才能用
    「快照行數」對佇列事件做計數去重（seq <= 快照長度 ⇒ 快照已含、跳過）。

    loop-safe：生產環境（uvicorn）全部連線共用一個事件迴圈，但 TestClient 每條 WS
    各開一個迴圈——跨迴圈對 asyncio.Queue 直接 put_nowait 不會喚醒對方迴圈的等待者。
    故 listener 記 (queue, 其所屬 loop)，投遞一律走 call_soon_threadsafe；attach 端
    的 interject 注入也經 hub.loop（session 所在迴圈）回拋。
    """

    def __init__(
        self,
        session_id: str,
        controller: object,
        queue: asyncio.Queue[str],
        *,
        live_only: bool = False,
    ) -> None:
        self.session_id = session_id
        self.controller = controller  # StudioSession：broadcast（插話回顯）/ request_stop
        self.queue = queue  # intervention queue（attach 端 interject 注入點）
        self.loop = asyncio.get_running_loop()  # session 所在迴圈（interject 回拋目標）
        self.seq = 0
        self.listeners: dict[asyncio.Queue, asyncio.AbstractEventLoop] = {}
        # live_only：improve umbrella 沒有單一 JSONL（各輪各自入檔），無快照可補放——
        # attach 只接「從現在起」的即時事件，seq==JSONL 行數不變式不適用（也不需要：
        # 無補放即無去重問題）。
        self.live_only = live_only

    def subscribe(self, q: asyncio.Queue) -> None:
        self.listeners[q] = asyncio.get_running_loop()

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.listeners.pop(q, None)

    def _deliver(self, item: object) -> None:
        for q, loop in tuple(self.listeners.items()):
            if q.qsize() >= _LISTENER_BACKLOG_MAX:
                self.listeners.pop(q, None)  # 慢消費者踢除，不拖慢討論主迴圈
                continue
            try:
                loop.call_soon_threadsafe(q.put_nowait, item)
            except RuntimeError:  # listener 的迴圈已關（連線已死）
                self.listeners.pop(q, None)

    def publish(self, d: dict) -> None:
        self.seq += 1
        self._deliver((self.seq, d))

    def close(self) -> None:
        """session 結束：對所有 listener 發結束哨兵並清空（done 事件已先 publish）。"""
        self._deliver(None)
        self.listeners.clear()

    def inject_interjection(self, text: str) -> None:
        """把 attach 端插話回拋到 session 所在迴圈（雙端回顯＋queue 注入）。

        順序刻意「先回顯、後注入」：回顯 task 會先於被喚醒的等待者執行到 record+publish
        （同一 ready queue 的先後），確保插話在 JSONL 與 fan-out 中先於專家對它的回應——
        注入先行時，等待中的專家可能瞬間跑完收尾，回顯反而落在 hub 關閉之後而遺失。
        """
        try:
            # 回顯走 controller.broadcast（入檔＋fan-out）；用 run_coroutine_threadsafe
            # 排回 session 迴圈執行，不在 attach 迴圈直接 await（跨迴圈 send 不安全）。
            asyncio.run_coroutine_threadsafe(
                self.controller.broadcast(events.human_message(self.session_id, text)),
                self.loop,
            )
            self.loop.call_soon_threadsafe(self.queue.put_nowait, text)
        except RuntimeError:
            pass  # session 迴圈已關（正在收尾）：插話已無意義，靜默略過


_hubs: dict[str, _SessionHub] = {}


async def _attach_session(websocket: WebSocket, data: dict) -> None:
    """把這條 WS 掛上進行中的 session：補放已錯過事件 → 接 live → 代收 interject/stop。"""
    sid = str(data.get("attach") or "").strip()
    try:
        cursor = max(0, int(data.get("cursor") or 0))
    except (TypeError, ValueError):
        cursor = 0
    hub = _hubs.get(sid)
    if hub is None:
        # improve 模式：前端記到的 session_id 是「當前這一輪」的 record sid，hub 只以
        # umbrella id 註冊——退而比對 controller._record_sid（同 stop_running 慣例）。
        hub = next(
            (h for h in _hubs.values() if getattr(h.controller, "_record_sid", None) == sid),
            None,
        )
    if hub is None:
        await websocket.send_json(
            {
                "type": "error",
                "payload": {
                    "code": "attach_unavailable",
                    "message": "該場討論不在進行中（已結束或服務已重啟），請從歷史重播",
                },
            }
        )
        await websocket.close()
        return
    if len(hub.listeners) >= _MAX_ATTACH_LISTENERS:
        await websocket.send_json(
            {
                "type": "error",
                "payload": {"code": "attach_unavailable", "message": "該場的旁聽連線已達上限"},
            }
        )
        await websocket.close()
        return

    q: asyncio.Queue = asyncio.Queue()
    hub.subscribe(q)  # 先訂閱（開始緩衝）……
    try:
        if hub.live_only:
            # improve umbrella 無單一 JSONL：不補放、只接即時事件（cursor 忽略、sent=0
            # 讓佇列事件全數通過）。斷線期間的事件可從各輪歷史查看。
            past: list[dict] = []
            sent = 0
        else:
            past = history.load_events(sid)  # ……後拍快照：快照必涵蓋訂閱前全部事件
            for d in past[cursor:]:
                await websocket.send_json(d)
            sent = len(past)
        # attach_ok 是 ws 層訊息（沿既有 raw error dict 先例），不進 events.py 契約；
        # cursor 回傳權威計數，讓前端事件計數器與伺服器校準。
        await websocket.send_json(
            {
                "type": "attach_ok",
                "payload": {
                    "session_id": hub.session_id,
                    "cursor": sent,
                    "live_only": hub.live_only,
                },
            }
        )

        async def _sender() -> None:
            nonlocal sent
            while True:
                item = await q.get()
                if item is None:
                    return  # session 已結束（done 已送）
                seq, d = item
                if seq <= sent:
                    continue  # 快照已含（訂閱先於快照造成的重疊），計數去重
                try:
                    await websocket.send_json(d)
                except (RuntimeError, WebSocketDisconnect):
                    return
                sent = seq

        sender = asyncio.create_task(_sender())
        try:
            # 與 _pump_interventions 同款：邊送邊收，interject/stop 餵回既有 controller。
            while not sender.done():
                recv = asyncio.ensure_future(websocket.receive_json())
                done, _pending = await asyncio.wait(
                    {recv, sender}, return_when=asyncio.FIRST_COMPLETED
                )
                if recv not in done:
                    recv.cancel()
                    break
                try:
                    msg = recv.result()
                except WebSocketDisconnect:
                    break
                kind = msg.get("type")
                if kind == "interject":
                    text = (msg.get("text") or "").strip()
                    if text:
                        # 注入＋回顯（入檔、原 socket 與所有 attach socket 都看得到）
                        # 統一經 hub 回拋 session 迴圈，跨迴圈安全。
                        hub.inject_interjection(text)
                elif kind == "stop":
                    hub.controller.request_stop()  # 純旗標，跨執行緒安全
        finally:
            sender.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await sender
    finally:
        hub.unsubscribe(q)
        with contextlib.suppress(RuntimeError):
            await websocket.close()


def stop_running(target_id: str) -> bool:
    """對進行中的討論／持續改良迴圈送停止指令；target 可為 session id 或專案 id。

    持續改良迴圈內每輪討論各有自己的 session id（improver._record_sid），註冊表只記
    umbrella id 與專案 id——直接命中不到時退而比對該欄位，讓「從歷史列表停掉正在跑
    的那一輪」也可行。回 True＝已送出停止（在安全點收尾，非立即中斷）；False＝沒有
    進行中的目標。
    """
    ctl = _running.get(target_id)
    if ctl is None:
        ctl = next(
            (c for c in _running.values() if getattr(c, "_record_sid", None) == target_id),
            None,
        )
    if ctl is None:
        return False
    ctl.request_stop()
    return True


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
    hub: _SessionHub | None = None  # 單場路徑建 session 後掛上（attach fan-out）

    async def broadcast(event: StudioEvent) -> None:
        nonlocal connected
        d = event.to_dict()
        if recording:
            history.record_event(session_id, d)
            # record 與 publish 之間不得插入 await（_SessionHub 的 seq==JSONL 行數不變式）。
            if hub is not None:
                hub.publish(d)
        elif hub is not None and hub.live_only:
            # improve umbrella：外層不開錄（各輪各自入檔），live-only hub 仍要 fan-out
            # 給 attach 的重連端（無補放故無 seq==JSONL 不變式需求）。
            hub.publish(d)
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
        # 斷線重掛：首訊息帶 attach 即訂閱既有進行中 session（不開新場、不占 slot）。
        if data.get("attach"):
            await _attach_session(websocket, data)
            return
        requirement = (data.get("requirement") or "").strip()
        repo_url = (data.get("repo_url") or "").strip()
        repo_branch = (data.get("repo_branch") or "").strip() or None
        project_id = (data.get("project_id") or "").strip()
        improve_mode = (data.get("mode") or "").strip() == "improve"
        group_name = (data.get("group") or "").strip()
        workflow_name = (data.get("workflow") or "").strip()

        project = projects.get(project_id) if project_id else None
        if project_id and project is None:
            await websocket.send_json({"type": "error", "payload": {"message": "找不到該專案"}})
            await websocket.close()
            return

        # 選用討論小組（可選）：以小組成員＋小組 mode 進行架構討論。未知名稱／設定檔損壞即早退。
        group = None
        if group_name:
            try:
                group = role_store.get_group(group_name)
            except role_store.GroupFileError as e:
                await websocket.send_json(
                    {"type": "error", "payload": {"message": f"討論小組設定檔損壞：{e}"}}
                )
                await websocket.close()
                return
            if group is None:
                await websocket.send_json(
                    {"type": "error", "payload": {"message": f"找不到討論小組「{group_name}」"}}
                )
                await websocket.close()
                return

        # 選用動態流程（可選）：未指定＝走內建預設骨架。未知名稱／設定檔損壞即早退。
        wf = None
        if workflow_name:
            try:
                wf = workflow.get_workflow(workflow_name)
            except workflow.WorkflowFileError as e:
                await websocket.send_json(
                    {"type": "error", "payload": {"message": f"動態流程設定檔損壞：{e}"}}
                )
                await websocket.close()
                return
            if wf is None:
                await websocket.send_json(
                    {"type": "error", "payload": {"message": f"找不到動態流程「{workflow_name}」"}}
                )
                await websocket.close()
                return
        # 互動 session 未指定 workflow → 走互動預設（config.DEFAULT_WORKFLOW，預設「動態優先」）。
        # improve（ProjectImprover，下方分支）與 autopilot（另一程序、直接建 StudioSession(workflow=None)）
        # 刻意維持安全骨架不受影響；OFFLINE 示範用決定性假專家、未為動態 stage 編腳本，亦維持安全骨架
        # ——故僅在「非 improve、非離線、未指定」時套互動預設。
        if wf is None and not improve_mode and not config.OFFLINE_MODE and config.DEFAULT_WORKFLOW:
            try:
                wf = workflow.get_workflow(config.DEFAULT_WORKFLOW)
            except workflow.WorkflowFileError:
                wf = None  # 設定檔壞掉→退回安全骨架（workflow=None），不擋使用者開工
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
                        "message": "專案模式不支援同時指定 repo 網址（專案已有固定 workspace）；"
                        "要在現有 GitHub repo 上工作，請在專案面板設定「目標 repo」"
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
            # improve 也可重掛：live-only hub（無單一 JSONL 可補放，只接續即時事件）。
            hub = _SessionHub(improver.session_id, improver, queue, live_only=True)
            _hubs[improver.session_id] = hub

            def _close_improve_hub(_t: asyncio.Task) -> None:
                _hubs.pop(improver.session_id, None)
                hub.close()

            run_task = asyncio.create_task(improver.run())
            run_task.add_done_callback(_close_improve_hub)
            run_task.add_done_callback(lambda _t: _release_session_slot())
            run_task.add_done_callback(lambda _t: _active_projects.discard(project["id"]))
            stop_keys = (improver.session_id, project["id"])
            _register_running(improver, *stop_keys)
            run_task.add_done_callback(lambda _t: _unregister_running(*stop_keys))
            await _pump_interventions(websocket, improver, queue, run_task)
            if not run_task.done():
                _detached.add(run_task)
                run_task.add_done_callback(_detached.discard)
            return

        if project is not None:
            # 專案固定 workspace：絕不清空，程式碼與 git 歷史跨場次累積。
            cwd = projects.workspace_dir(project["id"])
            # 目標 repo＝工作基底：專案自設 publish_repo 優先，否則退回全域 TI_PUBLISH_REPO
            # （與發佈端 fallback 對齊）。全新 workspace 先 clone 進來、已同源則快轉到遠端
            # base，讓專家「在設定的 repo 上做修改」而不是另起爐灶（詳見 repo_base）。
            base_repo = projects.effective_repo(project)
        else:
            cwd = workspace.create_workspace(session_id)
            # 未綁專案的一次性討論也以全域目標 repo 為基底（同樣與發佈端對齊）；使用者明確
            # 指定 repo_url 時尊重該意圖，不另外注入全域基底（避免與下方 clone 互踩）。
            base_repo = "" if repo_url else (config.PUBLISH_REPO or "").strip()
        # base_repo 為空（未設目標 repo／一次性 repo_url 路線）時直接 skipped，不空跑同步。
        base_sync = (
            await repo_base.ensure_base(cwd, base_repo, broadcast=broadcast, session_id=session_id)
            if base_repo
            else repo_base.SyncResult("skipped")
        )
        if base_sync.fatal:
            await websocket.send_json(
                {"type": "error", "payload": {"message": "工作基底同步失敗：" + base_sync.detail}}
            )
            await websocket.close()
            return

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
            publish_repo=(project.get("publish_repo") or None) if project is not None else None,
            # 僅在 workspace 確實同步自目標 repo 時告知 session（prompt 不對專家說謊）。
            base_repo=base_repo if base_sync.based else None,
            group=group,
            workflow=wf,
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
            else _run_plain_session(session, requirement)
        )
        # attach fan-out 樞紐：在 run_task 之前建好（recording=True 後至此無 broadcast，
        # 不會漏事件——第一個入檔事件是 run() 內的 session_started）。
        hub = _SessionHub(session_id, session, queue)
        _hubs[session_id] = hub

        def _close_hub(_t: asyncio.Task) -> None:
            _hubs.pop(session_id, None)
            hub.close()

        run_task = asyncio.create_task(run_coro)
        run_task.add_done_callback(_close_hub)
        # slot 隨 run_task 完成釋放（無論是否 detach、是否斷線），一次性、不重複。
        run_task.add_done_callback(lambda _t: _release_session_slot())
        if project is not None:
            pid = project["id"]
            run_task.add_done_callback(lambda _t: _active_projects.discard(pid))
        stop_keys = (session_id, project["id"]) if project is not None else (session_id,)
        _register_running(session, *stop_keys)
        run_task.add_done_callback(lambda _t: _unregister_running(*stop_keys))
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
    # 對齊長期方向的前綴（只進 session.run；history 標籤與 meta 足跡仍記原始需求）：
    # 有產品藍圖（TI_BLUEPRINT）用藍圖；否則退而用一句產品願景（澄清階段回填）。
    bp_ctx = blueprint.context(project["id"])
    vision = (project.get("vision") or "").strip()
    if bp_ctx:
        req = bp_ctx + requirement
    elif vision:
        req = f"【長期專案：{project['name']}】產品願景：{vision}\n\n{requirement}"
    else:
        req = requirement
    result = await session.run(req)
    sdir = projects.state_dir(project["id"])
    # 結果分流回填（雙軌路由單一決策點）：後續任務→專案 backlog；核心改動→核心 backlog，
    # 由 autopilot 在主核心 repo 實作開獨立 PR。單場討論同樣參與——下次持續改良迴圈現成供給。
    _added, routed = drain_result_to_backlogs(result, sdir)
    if routed:
        await session.broadcast(
            events.phase_change(
                session.session_id,
                "核心改動",
                f"已將 {routed} 項核心改動排入核心 repo（{config.CORE_REPO}）的改良佇列",
            )
        )
    # 立項抽出的願景回填專案 meta（僅當原本為空；下一場開場即可前綴）。
    new_vision = (result.get("vision") or "").strip()
    if new_vision and not vision:
        projects.update_vision(project["id"], new_vision)
    projects.record_session(
        project["id"], session.session_id, requirement[:80], bool(result.get("completed"))
    )
    return result


async def _run_plain_session(session: StudioSession, requirement: str) -> dict:
    """非專案的單場討論：無專案 backlog 可回填，但團隊判定的核心改動仍路由到主核心 repo
    （`核心改動:` 專指改 Ti 框架本身、與專案無關），由 autopilot 實作開獨立 PR。"""
    result = await session.run(requirement)
    routed = backlog.route_core_changes(result.get("core_changes") or [])
    if routed:
        await session.broadcast(
            events.phase_change(
                session.session_id,
                "核心改動",
                f"已將 {routed} 項核心改動排入核心 repo（{config.CORE_REPO}）的改良佇列",
            )
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
