"""專案持續改良迴圈 —— 讓團隊對同一個產品「一直找問題、一直改良」。

把 autopilot 的自我改善迴圈泛化到任意產品專案：
  取專案 backlog 的 pending 任務 → 跑一場完整討論（在專案的固定 workspace 上，
  程式碼與 git 歷史跨場次累積）→ 檢討發現的後續任務回填 backlog →
  backlog 空了就進「找問題」階段（資深專家審視產品現況、產出新改良任務）→ 下一輪。

與 autopilot 的差異：autopilot 是改 Ti 自己（含 push/merge/部署閘門）、由獨立服務跑；
improver 改的是使用者的產品專案，成果留在專案 workspace 的 git 歷史，經由 WebSocket
即時呈現給使用者，可隨時插話/停止。

每一輪（含「找問題」）各自記錄成獨立的 history session，可從歷史面板重播。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid

from . import (
    autopilot,
    backlog,
    blueprint,
    config,
    events,
    history,
    projects,
    repo_base,
    runner,
    workspace,
)
from .events import StudioEvent
from .orchestrator import StudioSession, parse_core_changes, parse_structured_tasks

log = logging.getLogger("ti.improver")

# 離線示範模式的「找問題」結論（無金鑰也能完整走一遍持續改良迴圈）。
# 完成過的標題會被去重過濾，所以第二次「找問題」自然回 0、迴圈收斂。
OFFLINE_DISCOVERY = [
    "為產品補上使用說明文件",
    "強化錯誤處理與輸入驗證",
]

# 離線示範模式的藍圖輸出（與 OFFLINE_DISCOVERY 同模式：讓 TI_OFFLINE=1 也能完整走
# 生成→解析→落盤→seed→注入的全流程）。
OFFLINE_BLUEPRINT = """願景: 做一個讓使用者輕鬆上手的示範產品
用戶: 想快速體驗持續改良迴圈的開發者
功能: [P0] 核心功能可運行 — 最小可用的主流程
功能: [P1] 使用說明文件
功能: [P2] 錯誤處理與輸入驗證
里程碑: M1 核心功能可運行
里程碑: M2 文件與穩健性補齊
"""

# 「找問題」單輪最多回填的任務數，避免一次把 backlog 塞爆。
DISCOVERY_MAX = 5

# 品質下限——與 autopilot 自評（#238）同源，複用 DISCOVERY_LOW_VALUE_TYPES 單一真相清單。
# 接到三視角「找問題」prompt 尾巴，讓工作室成員在**提案階段**就不輸出低價值/瑣碎任務，
# 避免它們進 backlog 跑完一輪才被當噪音刪掉（事後刪 → 源頭擋）。
_DISCOVERY_QUALITY_BAR = (
    "\n品質下限：只提『使用者可感知的具體缺陷或功能缺口』，每點須能指出證據（檔案:行號＋症狀或重現）。"
    "以下低價值類型一律不要輸出；高價值點不足時寧可只給 1~2 點，嚴禁充數：\n"
    + autopilot.DISCOVERY_LOW_VALUE_TYPES
)


def drain_result_to_backlogs(result: dict, project_state_dir) -> tuple[int, int]:
    """把一場討論結果分流回填 backlog，回傳 (回填的後續任務數, 路由的核心改動數)。

    雙軌路由的單一決策點（見 ARCHITECTURE.md「專案 repo 與 Ti 主核心 repo」）：
      - 後續任務（`後續任務:`）→ 專案 backlog（`project_state_dir`），迴圈自我補給。
        優先用含 priority/type 的結構化版本；舊 result（無 followup_items）退回純標題。
      - 核心改動（`核心改動:`）→ 核心 backlog（見 backlog.route_core_changes，含近期完成去重）。
    """
    items = result.get("followup_items") or []
    followups = result.get("followups") or []
    if items:
        added = backlog.add_items(items, source="discovered", state_dir=project_state_dir)
    else:
        added = backlog.add_many(followups, source="discovered", state_dir=project_state_dir)
    return added, backlog.route_core_changes(result.get("core_changes") or [])


class ProjectImprover:
    """單一專案的持續改良迴圈。介面與 StudioSession 對齊（session_id / broadcast /
    request_stop），讓 ws._pump_interventions 不需分支即可共用插話/停止管線。"""

    def __init__(
        self,
        project: dict,
        broadcast,
        intervention_queue: asyncio.Queue[str] | None = None,
    ):
        self.project = project
        self.outer_broadcast = broadcast
        self.queue = intervention_queue
        # 給插話回顯（human_message）用的 umbrella id；各輪討論另有自己的 session id。
        self.session_id = f"improve-{project['id']}"
        self._stop = False
        self._current: StudioSession | None = None
        self._record_sid: str | None = None  # 目前要把事件記到哪個 history session

    # --- 控制（與 StudioSession 同名介面）-------------------------------
    def request_stop(self) -> None:
        self._stop = True
        if self._current is not None:
            self._current.request_stop()

    async def broadcast(self, event: StudioEvent) -> None:
        """轉送到前端，並記錄到目前這一輪的 history session（若有）。"""
        if self._record_sid:
            history.record_event(self._record_sid, event.to_dict())
        await self.outer_broadcast(event)

    # --- 主迴圈 ----------------------------------------------------------
    async def run(self, max_cycles: int | None = None) -> dict:
        """跑持續改良迴圈，回傳摘要 {cycles, done, failed, stopped}。

        結束條件（先到先停）：使用者停止／達 max_cycles（0=不限）／連續失敗達上限／
        backlog 空且「找問題」找不出新改善點（自然收斂）。
        """
        pid = self.project["id"]
        sdir = projects.state_dir(pid)
        limit = config.IMPROVE_MAX_CYCLES if max_cycles is None else max_cycles
        summary = {"cycles": 0, "done": 0, "failed": 0, "stopped": False}
        consecutive_fails = 0

        # 開跑前先把工作基底同步到目標 repo（若有設定）——必須趕在藍圖 commit 之前，
        # 否則 pristine workspace 會先長出獨立 root commit，與目標 repo 永遠分歧。
        base_sync = await repo_base.ensure_base(
            projects.workspace_dir(pid),
            projects.effective_repo(self.project),
            broadcast=self.broadcast,
            session_id=self.session_id,
        )
        if base_sync.fatal:
            # 全新 workspace 拿不到基底＝開工只會製造無共同歷史的孤兒成果，
            # 不啟動迴圈；走標準 DONE 收尾（stopped=True），前端不會懸著。
            await self.broadcast(
                events.phase_change(
                    self.session_id, "持續改良", "工作基底同步失敗，迴圈未啟動：" + base_sync.detail
                )
            )
            self._stop = True

        # 開跑前先備妥產品藍圖（每專案僅生成一次；失敗/解析不出時降級續行，絕不擋迴圈）。
        if config.BLUEPRINT_ENABLED and not self._stop and not blueprint.exists(pid):
            await self._ensure_blueprint()

        while not self._stop and (limit <= 0 or summary["cycles"] < limit):
            task = backlog.next_pending(state_dir=sdir)
            if task is None:
                n = await self._discover(sdir)
                if self._stop or n == 0:
                    break  # 找不到新改善點：迴圈自然收斂
                continue

            summary["cycles"] += 1
            completed = await self._run_task(task, sdir)
            if completed:
                summary["done"] += 1
                consecutive_fails = 0
            else:
                summary["failed"] += 1
                consecutive_fails += 1
                if consecutive_fails >= config.IMPROVE_MAX_FAILS:
                    await self.broadcast(
                        events.phase_change(
                            self.session_id,
                            "持續改良",
                            f"連續 {consecutive_fails} 輪未完成，暫停迴圈待人工檢視",
                        )
                    )
                    break
            if config.IMPROVE_COOLDOWN > 0 and not self._stop:
                await asyncio.sleep(config.IMPROVE_COOLDOWN)

        summary["stopped"] = self._stop
        counts = backlog.counts(state_dir=sdir)
        await self.broadcast(
            StudioEvent(
                events.EventType.DONE,
                self.session_id,
                {
                    "completed": summary["failed"] == 0 and summary["cycles"] > 0,
                    "stopped": self._stop,
                    "improve": {**summary, "backlog": counts},
                },
            )
        )
        return summary

    # --- 單輪：跑一場討論 -------------------------------------------------
    async def _run_task(self, task: dict, sdir) -> bool:
        pid = self.project["id"]
        sid = "pj" + uuid.uuid4().hex[:10]
        backlog.set_status(task["id"], "in_progress", state_dir=sdir, session_id=sid)
        name = self.project.get("name", pid)
        history.start_session(sid, f"[專案 {name}] {task['title']}")
        self._record_sid = sid
        await self.broadcast(
            events.phase_change(
                self.session_id, "持續改良", f"第 {task['id']} 號改良任務：{task['title']}"
            )
        )

        cwd = projects.workspace_dir(pid)
        # 每輪先同步工作基底：上一輪的 PR 合併後，這裡把本地 base 快轉回目標 repo 最新狀態。
        # 目標 repo 同樣 fallback 全域 TI_PUBLISH_REPO（與發佈端對齊，見 projects.effective_repo）。
        base_repo = projects.effective_repo(self.project)
        base_sync = await repo_base.ensure_base(
            cwd, base_repo, broadcast=self.broadcast, session_id=self.session_id
        )
        if base_sync.fatal:
            backlog.set_status(
                task["id"],
                "failed",
                state_dir=sdir,
                note="工作基底同步失敗：" + base_sync.detail,
            )
            history.finish_session(sid)
            self._record_sid = None
            return False
        requirement = self._compose_requirement(task)
        experts = critics = None
        if config.OFFLINE_MODE:
            from .fake_experts import build_fake_critics, build_fake_experts

            experts = build_fake_experts(sid, cwd, requirement)
            critics = build_fake_critics(sid, cwd)
        session = StudioSession(
            sid,
            self.broadcast,
            experts=experts,
            cwd=cwd,
            intervention_queue=self.queue,
            critics=critics,
            workspace_id=projects.workspace_id(pid),
            clarify=False,  # 自主迴圈不反問：任務來自 backlog／找問題，沒有人在等著回答
            publish_repo=self.project.get("publish_repo") or None,
            base_repo=base_repo if base_sync.based else None,
        )
        if config.OFFLINE_MODE:
            from .fake_experts import build_fake_lane_expert

            session._lane_expert_factory = build_fake_lane_expert
        self._current = session
        try:
            result = await session.run(requirement)
        finally:
            self._current = None
            history.finish_session(sid)
            self._record_sid = None

        completed = bool(result.get("completed"))
        # 一場討論結果分流回填：後續任務→專案 backlog；核心改動→核心 backlog（見雙軌路由）。
        added, routed = drain_result_to_backlogs(result, sdir)
        if added:
            log.info("專案 %s 從討論回填 %d 個後續任務", pid, added)
        if routed:
            log.info(
                "專案 %s 路由 %d 個核心改動到核心 backlog（%s）", pid, routed, config.CORE_REPO
            )
            await self.broadcast(
                events.phase_change(
                    self.session_id,
                    "核心改動",
                    f"已將 {routed} 項核心改動排入核心 repo（{config.CORE_REPO}）的改良佇列",
                )
            )
        backlog.set_status(
            task["id"],
            "done" if completed else "failed",
            state_dir=sdir,
            note="" if completed else "討論未達完成",
        )
        projects.record_session(pid, sid, task["title"], completed)
        return completed

    def _compose_requirement(self, task: dict) -> str:
        name = self.project.get("name", "")
        vision = self.project.get("vision", "")
        parts = [f"【長期專案：{name}】這是持續改良中的既有產品，不是從零開始的新專案。"]
        if vision:
            parts.append(f"產品願景：{vision}")
        bp_ctx = blueprint.context(self.project["id"])
        if bp_ctx:
            parts.append(bp_ctx.rstrip())
        sc_ctx = self._scorecard_context()
        if sc_ctx:
            parts.append(sc_ctx.rstrip())
        parts.append(f"本輪改良任務：{task['title']}")
        if task.get("detail"):
            parts.append(f"細節：{task['detail']}")
        parts.append(
            "工作目錄裡是這個產品的既有程式碼與 git 歷史（首輪可能為空），"
            "請先瀏覽現況再拆解與動工；改良要與既有架構一致，不要砍掉重練。"
        )
        return "\n\n".join(parts)

    # --- 產品藍圖：開跑前 PM 把一句願景展開成結構化藍圖 ----------------------
    async def _ensure_blueprint(self) -> None:
        """PM 生成產品藍圖：落盤 blueprint.json＋BLUEPRINT.md、功能餵 backlog。

        解析不出結構時降級：原文仍寫 BLUEPRINT.md（人讀價值保留）、json 標記 raw、
        不餵 backlog——行為退回現狀，絕不擋持續改良迴圈。
        """
        pid = self.project["id"]
        name = self.project.get("name", pid)
        sid = "pjbp" + uuid.uuid4().hex[:9]
        history.start_session(sid, f"[專案 {name}] 產品藍圖：PM 展開願景")
        self._record_sid = sid
        await self.broadcast(
            events.phase_change(self.session_id, "產品藍圖", "PM 正在把願景展開成產品藍圖")
        )
        try:
            if config.OFFLINE_MODE:
                text = OFFLINE_BLUEPRINT
            else:
                text = await self._blueprint_with_pm(pid, sid)
            data = blueprint.parse_blueprint(text)
            if data is None:
                blueprint.write_md(pid, text)
                blueprint.save(pid, {"version": 1, "features": [], "raw": True}, session_id=sid)
                note = "藍圖輸出無法解析，已存原文（不餵 backlog）"
            else:
                seeded = blueprint.seed_backlog(pid, data, config.BLUEPRINT_SEED_MAX)
                blueprint.save(pid, data, session_id=sid)  # seed 後存，保住 seeded 標記
                blueprint.write_md(pid, blueprint.render_md(data, name=name))
                cwd = projects.workspace_dir(pid)
                await runner.git_init(cwd)  # 首輪 workspace 可能還沒 repo；冪等
                await runner.git_commit(cwd, "產品藍圖：PM 展開願景")
                note = f"藍圖完成：{len(data['features'])} 項功能，{seeded} 項已排入 backlog"
            history.record_event(
                sid,
                StudioEvent(
                    events.EventType.DONE, sid, {"completed": True, "blueprint": True}
                ).to_dict(),
            )
        except Exception:
            log.exception("專案 %s 產品藍圖生成失敗，降級為無藍圖續行", pid)
            note = "藍圖生成失敗，按原流程續行"
        finally:
            history.finish_session(sid)
            self._record_sid = None
        await self.broadcast(events.phase_change(self.session_id, "產品藍圖", note))

    async def _blueprint_with_pm(self, pid: str, sid: str) -> str:
        from .providers import make_expert
        from .roles import PM

        cwd = projects.workspace_dir(pid)
        expert = make_expert(PM, sid, cwd)
        vision = self.project.get("vision", "")
        prompt = (
            f"你要為長期產品專案「{self.project.get('name', '')}」制定產品藍圖"
            "（工作目錄是它的 workspace，首輪可能為空）。\n"
            + (f"產品願景：{vision}\n" if vision else "")
            + "請把願景展開成結構化藍圖，逐行輸出、格式固定為：\n"
            "願景: <一句精煉的產品願景>\n"
            "用戶: <目標用戶與使用場景，一句>\n"
            "功能: [P0] <功能名> — <一句說明>（P0 必須有/P1 重要/P2 加分，共 5~10 項）\n"
            "里程碑: M1 <第一個可用版本包含哪些功能>\n"
            "里程碑: M2 <下一階段>\n"
            "只輸出上述格式行，不要其他說明。"
        )
        try:
            return await expert.speak(prompt, self.broadcast)
        finally:
            with contextlib.suppress(Exception):
                await expert.stop()

    # --- 找問題：backlog 空了就審視產品、產出新改良任務 ---------------------
    async def _discover(self, sdir) -> int:
        """資深專家審視產品現況、產出改良任務寫進 backlog，回傳新增數。

        會把專案近期成敗回饋給專家（避免重提已完成、避開已知失敗做法）；
        產出再以「近期已完成標題」去重，防止剛做完又被重新提出。
        """
        pid = self.project["id"]
        name = self.project.get("name", pid)
        sid = "pjd" + uuid.uuid4().hex[:9]
        history.start_session(sid, f"[專案 {name}] 找問題：審視產品提出改良點")
        self._record_sid = sid
        await self.broadcast(
            events.phase_change(
                self.session_id, "找問題", "團隊多視角審視產品、找改良點（工程/產品/調研）"
            )
        )
        try:
            if config.OFFLINE_MODE:
                items = [
                    {"title": t, "priority": 1, "type": "improvement"} for t in OFFLINE_DISCOVERY
                ]
            else:
                items = await self._discover_with_experts(pid, sid)
            # 只寫進 history（不 broadcast）：讓這個審視 session 在歷史面板顯示為「完成」，
            # 又不會在前端被誤當成一輪改良的結束。
            history.record_event(
                sid,
                StudioEvent(
                    events.EventType.DONE, sid, {"completed": True, "discovery": True}
                ).to_dict(),
            )
        finally:
            history.finish_session(sid)
            self._record_sid = None

        raw_n = len(items)
        done_titles = backlog.recent_done_titles(config.AUTOPILOT_EVAL_MEMORY, state_dir=sdir)
        # done 防線：與 pending 同構——精確比對升級為相似度比對，複用 autopilot._first_similar_title
        # （詞集 Jaccard ≥ AUTOPILOT_DEDUP_RATIO），攔得住改寫過的重提。前半段真值守衛保留：空標題
        # 應由真值檢查擋掉，不流進 helper。AUTOPILOT_EVAL_MEMORY=0 時 done_titles 為空 corpus，helper
        # 全回 None，與舊精確比對關閉行為逐位等價，向後相容。
        items = [
            t
            for t in items
            if t["title"].strip()
            and autopilot._first_similar_title(t["title"].strip(), done_titles) is None
        ]
        # 進場 pre-filter：與 autopilot 自評同一把關（複用 _filter_pending_duplicates 兩道防線——
        # 相似度去重 + 子系統廣度），丟掉與專案 backlog 既有 pending/in_progress 語意相近、或同子系統
        # 已過多的提案。連同上一行 done 去重，把瑣碎/重複任務擋在進 backlog 之前（源頭擋）。
        existing = [
            t["title"]
            for t in backlog.list_tasks(state_dir=sdir)
            if t["status"] in ("pending", "in_progress")
        ]
        kept = set(
            autopilot._filter_pending_duplicates([t["title"].strip() for t in items], existing)
        )
        items = [t for t in items if t["title"].strip() in kept]
        n = backlog.add_items(items[:DISCOVERY_MAX], source=self._discovery_source(), state_dir=sdir)
        # 留痕閉環：把「源頭擋掉幾個重複/低價值提案」回報前端並寫進伺服器日誌，取代靜默丟棄，
        # 讓使用者看得到把關量、可據此回饋調整品質下限（dropped 不含 DISCOVERY_MAX 容量截斷）。
        dropped = raw_n - len(items)
        msg = f"提出 {n} 個新改良任務" if n else "本輪未找出新的改良點"
        if dropped:
            msg += f"（源頭擋掉 {dropped} 個重複/低價值提案）"
        log.info("找問題：提案 %d、過濾丟棄 %d、入列 %d", raw_n, dropped, n)
        await self.broadcast(events.phase_change(self.session_id, "找問題", msg))
        return n

    def _discover_role_keys(self) -> list[str]:
        """解析 TI_DISCOVER_ROLES：過濾未知鍵；可選角色須仍在 OPTIONAL_ROLES（被關即降級）。"""
        from .roles import BY_KEY, CORE_ROLES

        core = {r.key for r in CORE_ROLES}
        keys: list[str] = []
        for key in config.DISCOVER_ROLES:
            if key not in BY_KEY or key in keys:
                continue
            if key in core or key in config.OPTIONAL_ROLES:
                keys.append(key)
        return keys or ["senior"]  # 全被過濾時保底單視角，找問題階段不致空轉

    def _discover_prompts(self, pid: str) -> dict[str, str]:
        """各視角的「找問題」prompt。共用成績單前綴、藍圖脈絡與結構化 `任務:` 輸出格式。"""
        name = self.project.get("name", "")
        vision = self.project.get("vision", "")
        # 長期目標段與 autopilot 自評同源（autopilot.north_star_context → config，單一真相）。
        head = (
            autopilot.north_star_context()
            + self._intent_context()  # 意圖差距分析與 generic 同步注入(F2:蓋章 source=intent 的前提)
            + self._recent_outcomes_context()
            + self._scorecard_context()
            + (
                f"你正在審視長期產品專案「{name}」（程式碼就在你的工作目錄）。\n"
                + (f"產品願景：{vision}\n" if vision else "")
                + blueprint.context(pid)
            )
        )
        tail = (
            "找出最值得改良的 3~5 點，每點獨立一行，格式固定為 "
            "`任務: [P0/bug] <動詞開頭的具體任務>`——方括號標籤標注優先級"
            "（P0 必須~P2 加分）與類型（feature/bug/improvement），標籤可省（視為 P1）。\n"
            "若發現的是「要改 Ti 核心框架本身（orchestrator／runner／發佈流程等），而非本產品的"
            "程式碼」，請改用 `核心改動: <描述>` 另行列出（會路由到 Ti 主核心 repo、不混進本專案）。"
            "只輸出任務行或核心改動行。" + _DISCOVERY_QUALITY_BAR
        )
        wid = projects.workspace_id(pid)
        prd_tail = workspace.read_prd_tail(wid, config.KNOWLEDGE_MAX_CHARS)
        research_tail = workspace.read_doc_tail(wid, "RESEARCH.md", config.KNOWLEDGE_MAX_CHARS)
        return {
            "senior": head
            + "請用 Read/Grep 瀏覽現況，從使用者價值與工程品質兩面（功能缺口、bug、體驗、"
            "測試、安全）" + tail,
            "pm": head
            + (f"【PRD（需求澄清沉澱）】\n{prd_tail}\n\n" if prd_tail else "")
            + "請用 Read/Grep 瀏覽現況，從目標用戶與產品價值的角度（功能缺口、使用體驗、"
            "與願景的落差）" + tail,
            "researcher": head
            + (f"【既有調研（docs/RESEARCH.md）】\n{research_tail}\n\n" if research_tail else "")
            + "請先沿用上面的既有調研，再上網看同類產品與業界最佳實踐，從「我們還缺什麼能力」"
            "的角度" + tail,
        }

    def _intent_context(self) -> str:
        """意圖迴路(第 4 階 B3):把專案常駐 intent 變成找問題的差距分析指令。

        每次現讀 meta(不用 self.project 快照)——intent 是「可隨時更新的指令」,
        使用者改了下一輪就要生效。旗標關/無 intent 回空字串=零行為變更。
        """
        if not config.INTENT_LOOP:
            return ""
        meta = projects.get(self.project.get("id", "")) or self.project
        intent = str(meta.get("intent") or "").strip()
        if not intent:
            return ""
        return (
            f"【專案常駐意圖(北極星指令)】{intent}\n"
            "請先做差距分析:對照上述意圖與產品現況/近期完成項,優先提出「離意圖最近的"
            "缺口」任務;與意圖無關的鍍金式改良不要提。\n"
        )

    def _discovery_source(self) -> str:
        """本輪找問題的 backlog source:意圖差距分析驅動=「intent」,否則「eval」。

        第 4 階量測(軌 F2)靠這個標記把「意圖→交付」從一般自我發現中辨識出來;
        _intent_context 為空(旗標關/無 intent)時與舊行為逐位等價。
        """
        return "intent" if self._intent_context() else "eval"

    async def _discover_with_experts(self, pid: str, sid: str) -> list[dict]:
        """多視角並行「找問題」：各視角獨立提案 → 角色輪替合併＋依標題去重。

        輪替合併（senior[0], pm[0], researcher[0], senior[1]…）保證 DISCOVERY_MAX 截斷後
        每個視角至少有代表進 backlog。產出為結構化任務（含 priority/type，#95 格式）；
        研究員產出順手沉澱 docs/RESEARCH.md（與正式流程同管道）。
        """
        from .providers import make_expert
        from .roles import BY_KEY

        cwd = projects.workspace_dir(pid)
        keys = self._discover_role_keys()
        prompts = self._discover_prompts(pid)
        generic = (
            autopilot.north_star_context()
            + self._intent_context()
            + self._recent_outcomes_context()
            + self._scorecard_context()
            + f"你正在審視長期產品專案「{self.project.get('name', '')}」。"
            "請從你的專業視角找出最值得改良的 3~5 點，每點獨立一行，"
            "格式固定為 `任務: [P0/bug] <動詞開頭的具體任務>`（標籤可省，視為 P1）；"
            "若是要改 Ti 核心框架本身（非本產品程式碼）則改用 `核心改動: <描述>`（路由到主核心 repo）。"
            "只輸出任務行或核心改動行。" + _DISCOVERY_QUALITY_BAR
        )

        core_buf: list[
            dict
        ] = []  # 找問題時辨識出的 Ti 核心議題（與專案任務分流，稍後路由核心 repo）

        async def _ask(key: str) -> list[dict]:
            expert = make_expert(BY_KEY[key], f"{sid}:{key}" if len(keys) > 1 else sid, cwd)
            try:
                text = await expert.speak(prompts.get(key, generic), self.broadcast)
            except Exception:  # noqa: BLE001 — 單一視角失敗不拖垮整個找問題階段
                return []
            finally:
                with contextlib.suppress(Exception):
                    await expert.stop()
            if key == "researcher" and config.KNOWLEDGE_ENABLED:
                workspace.append_doc(projects.workspace_id(pid), "RESEARCH.md", text)
            # 單執行緒 asyncio：append 在 await 之後同步進行，視角間不會競態。
            core_buf.extend(parse_core_changes(text))
            return parse_structured_tasks(text)

        proposals = await asyncio.gather(*(_ask(k) for k in keys))
        # 找問題若辨識出 Ti 核心議題，與專案任務分流——路由到核心 backlog（依標題去重），不進專案
        # backlog；由 autopilot 在主核心 repo 實作開獨立 PR（雙軌路由，見 backlog.route_core_changes）。
        if core_buf:
            uniq, seen_core = [], set()
            for c in core_buf:
                if c["title"] not in seen_core:
                    seen_core.add(c["title"])
                    uniq.append(c)
            routed = backlog.route_core_changes(uniq)
            if routed:
                log.info("找問題：路由 %d 個核心改動到核心 backlog（%s）", routed, config.CORE_REPO)
                await self.broadcast(
                    events.phase_change(
                        self.session_id,
                        "核心改動",
                        f"找問題辨識出 {routed} 項核心改動，已排入核心 repo（{config.CORE_REPO}）佇列",
                    )
                )
        # 角色輪替合併 + 依標題去重（recent_done 過濾與 DISCOVERY_MAX 截斷由呼叫端負責）。
        merged: list[dict] = []
        seen: set[str] = set()
        for i in range(max((len(p) for p in proposals), default=0)):
            for p in proposals:
                if i < len(p):
                    title = p[i]["title"].strip()
                    if title and title not in seen:
                        seen.add(title)
                        merged.append(p[i])
        return merged

    def _recent_outcomes_context(self) -> str:
        """專案近期成敗（done/failed＋原因）整理成提示前綴；無紀錄回空字串。"""
        sdir = projects.state_dir(self.project["id"])
        limit = config.AUTOPILOT_EVAL_MEMORY
        if limit <= 0:
            return ""

        def _recent(status: str) -> list[dict]:
            rows = sorted(
                backlog.list_tasks(status, state_dir=sdir),
                key=lambda t: t.get("updated_at", 0),
                reverse=True,
            )
            return rows[:limit]

        done, failed = _recent("done"), _recent("failed")
        if not done and not failed:
            return ""
        lines = ["【本專案過往成績單（請據此提出全新、不重複的改良點）】"]
        if done:
            lines.append("✅ 近期已完成（請勿重複提出）：")
            lines += [f"- {t['title'].strip()}" for t in done]
        if failed:
            lines.append("❌ 近期失敗（除非有明確不同的新做法，否則勿重蹈覆轍）：")
            for t in failed:
                note = (t.get("note") or "").strip()
                lines.append(f"- {t['title'].strip()}" + (f" — {note}" if note else ""))
        return "\n".join(lines) + "\n\n"

    # 退回原因 key → 繁中描述（scorecard.rejects 的欄位契約見 history._derive_scorecard）
    _REJECT_LABELS = {
        "qa_fail": "QA 驗證失敗",
        "smoke_fail": "自測失敗",
        "gate_veto": "客觀閘門退回",
        "critic": "異議退回",
        "stall": "停滯收斂",
    }

    def _scorecard_context(self) -> str:
        """本專案近 N 場的量化成績單摘要（roadmap 階段三：記分卡回饋進流程）。

        取專案 meta 的 sessions 尾 N 場（N＝AUTOPILOT_EVAL_MEMORY）→ history meta →
        aggregate_scorecard 聚合成一~三行繁中提示。無資料或任何失敗一律回空字串——
        回饋只是優化，絕不擋改良迴圈；輸出不得含 `任務:`/`核心改動:` 等 marker 字樣。
        """
        try:
            limit = config.AUTOPILOT_EVAL_MEMORY
            if limit <= 0:
                return ""
            recorded = (projects.get(self.project["id"]) or {}).get("sessions") or []
            metas = []
            for row in recorded[-limit:]:
                meta = history.get_meta(row.get("session_id", ""))
                if meta and isinstance(meta.get("scorecard"), dict):
                    metas.append(meta)
            if not metas:
                return ""
            metas.sort(key=lambda m: m.get("started_at", 0), reverse=True)  # 聚合契約：新→舊
            agg = history.aggregate_scorecard(metas)
            if not agg.get("n"):
                return ""

            def _pct(v: float | None) -> str:
                return f"{round(v * 100)}%" if v is not None else "—"

            line1 = (
                f"【本專案近 {agg['n']} 場量化成績單】完成率 {_pct(agg.get('completed_rate'))}、"
                f"QA 通過率 {_pct(agg.get('qa_pass_rate'))}"
            )
            if agg.get("avg_rounds") is not None:
                line1 += f"、平均輪數 {agg['avg_rounds']}"
            lines = [line1 + "。"]
            rejects = [
                f"{self._REJECT_LABELS[k]} {v} 次"
                for k, v in (agg.get("rejects") or {}).items()
                if v and k in self._REJECT_LABELS
            ]
            if rejects:
                lines.append(f"退回主因：{'、'.join(rejects)}。找問題請優先對準上述弱項。")
            return "\n".join(lines)[:300] + "\n\n"
        except Exception:  # noqa: BLE001 — 記分卡回饋失敗不得擋改良迴圈
            return ""
