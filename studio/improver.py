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

from . import backlog, config, events, history, projects, workspace
from .events import StudioEvent
from .orchestrator import StudioSession, parse_tasks

log = logging.getLogger("ti.improver")

# 離線示範模式的「找問題」結論（無金鑰也能完整走一遍持續改良迴圈）。
# 完成過的標題會被去重過濾，所以第二次「找問題」自然回 0、迴圈收斂。
OFFLINE_DISCOVERY = [
    "為產品補上使用說明文件",
    "強化錯誤處理與輸入驗證",
]

# 「找問題」單輪最多回填的任務數，避免一次把 backlog 塞爆。
DISCOVERY_MAX = 5


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
        # 檢討發現的後續任務回填專案 backlog —— 迴圈的自我補給線。
        followups = result.get("followups") or []
        if followups:
            added = backlog.add_many(followups, source="discovered", state_dir=sdir)
            if added:
                log.info("專案 %s 從討論回填 %d 個後續任務", pid, added)
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
        parts.append(f"本輪改良任務：{task['title']}")
        if task.get("detail"):
            parts.append(f"細節：{task['detail']}")
        parts.append(
            "工作目錄裡是這個產品的既有程式碼與 git 歷史（首輪可能為空），"
            "請先瀏覽現況再拆解與動工；改良要與既有架構一致，不要砍掉重練。"
        )
        return "\n\n".join(parts)

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
                titles = list(OFFLINE_DISCOVERY)
            else:
                titles = await self._discover_with_experts(pid, sid)
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

        done_titles = backlog.recent_done_titles(config.AUTOPILOT_EVAL_MEMORY, state_dir=sdir)
        titles = [t for t in titles if t.strip() and t.strip() not in done_titles]
        n = backlog.add_many(titles[:DISCOVERY_MAX], source="eval", state_dir=sdir)
        await self.broadcast(
            events.phase_change(
                self.session_id,
                "找問題",
                f"提出 {n} 個新改良任務" if n else "本輪未找出新的改良點",
            )
        )
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
        """各視角的「找問題」prompt。共用近期成績單前綴與 `任務:` 輸出格式。"""
        name = self.project.get("name", "")
        vision = self.project.get("vision", "")
        head = self._recent_outcomes_context() + (
            f"你正在審視長期產品專案「{name}」（程式碼就在你的工作目錄）。\n"
            + (f"產品願景：{vision}\n" if vision else "")
        )
        tail = (
            "找出最值得改良的 3~5 點，每點獨立一行，格式固定為 "
            "`任務: <動詞開頭的具體任務>`。只輸出任務行。"
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

    async def _discover_with_experts(self, pid: str, sid: str) -> list[str]:
        """多視角並行「找問題」：各視角獨立提案 → 角色輪替合併＋exact 去重。

        輪替合併（senior[0], pm[0], researcher[0], senior[1]…）保證 DISCOVERY_MAX 截斷後
        每個視角至少有代表進 backlog。研究員產出順手沉澱 docs/RESEARCH.md（與正式流程同管道）。
        """
        from .providers import make_expert
        from .roles import BY_KEY

        cwd = projects.workspace_dir(pid)
        keys = self._discover_role_keys()
        prompts = self._discover_prompts(pid)
        generic = (
            self._recent_outcomes_context()
            + f"你正在審視長期產品專案「{self.project.get('name', '')}」。"
            "請從你的專業視角找出最值得改良的 3~5 點，每點獨立一行，"
            "格式固定為 `任務: <動詞開頭的具體任務>`。只輸出任務行。"
        )

        async def _ask(key: str) -> list[str]:
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
            return parse_tasks(text)

        proposals = await asyncio.gather(*(_ask(k) for k in keys))
        # 角色輪替合併 + exact 去重（recent_done 過濾與 DISCOVERY_MAX 截斷由呼叫端負責）。
        merged: list[str] = []
        seen: set[str] = set()
        for i in range(max((len(p) for p in proposals), default=0)):
            for p in proposals:
                if i < len(p):
                    t = p[i].strip()
                    if t and t not in seen:
                        seen.add(t)
                        merged.append(t)
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
