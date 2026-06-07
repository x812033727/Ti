"""StudioSession — 工作室的討論/工作流程狀態機（核心）。

Phase 2 流程：PM 拆解結構化任務 → 架構辯論（工程師⇄高級工程師）→ 逐任務迭代
（實作→交付前自測→驗證→審查→帶意見改進，每任務最多 TASK_MAX_ROUNDS 輪）→ 最終實際 Demo
→ PM 驗收 → 團隊檢討。支援人類中途插話與停止。每一步都透過 broadcast callback 送事件。

為了可測試，experts 以 dict 注入；確定性執行（跑程式 / git）集中在 runner，cwd=None 時跳過。
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Protocol

from . import config, events, publisher, runner, workspace
from .roles import ROSTER, Role

Broadcast = Callable[[events.StudioEvent], Awaitable[None]]


class ExpertLike(Protocol):
    role: Role

    async def speak(self, prompt: str, broadcast: Broadcast) -> str: ...
    async def stop(self) -> None: ...


# --- 決議解析 -----------------------------------------------------------


def _last_match(text: str, pattern: str) -> str | None:
    matches = re.findall(pattern, text)
    return matches[-1].strip() if matches else None


def qa_passed(text: str) -> bool:
    verdict = _last_match(text, r"驗證\s*[:：]\s*(PASS|FAIL)")
    if verdict:
        return verdict.upper() == "PASS"
    # 後備：找不到標記時，看是否出現失敗字樣
    return not re.search(r"\b(fail|failed|error|錯誤|失敗)\b", text, re.I)


def senior_approved(text: str) -> bool:
    verdict = _last_match(text, r"決議\s*[:：]\s*(核可|退回)")
    if verdict:
        return verdict == "核可"
    return not re.search(r"(退回|需修改|必須修正)", text)


def security_approved(text: str) -> bool:
    verdict = _last_match(text, r"決議\s*[:：]\s*(安全核可|安全退回)")
    if verdict:
        return verdict == "安全核可"
    return not re.search(r"(安全退回|高風險|不安全|漏洞|injection)", text, re.I)


def pm_done(text: str) -> bool:
    verdict = _last_match(text, r"決議\s*[:：]\s*(完成|未完成)")
    if verdict:
        return verdict == "完成"
    return bool(re.search(r"(已完成|達成|符合驗收)", text))


def parse_tasks(pm_text: str) -> list[str]:
    """從 PM 的拆解文字抽出任務條目。優先 `任務: ...`，否則退回條列項目。"""
    explicit = [m.strip() for m in re.findall(r"^\s*任務\s*[:：]\s*(.+)$", pm_text, re.M)]
    if explicit:
        return explicit[:12]
    tasks: list[str] = []
    for line in pm_text.splitlines():
        m = re.match(r"^\s*(?:[-*•]|\d+[.)、])\s+(.*)$", line)
        if m:
            item = m.group(1).strip()
            if item and len(item) < 200 and not re.search(r"(執行指令|執行命令)", item):
                tasks.append(item)
    return tasks[:12] or ["實作需求"]


def parse_followups(text: str) -> list[str]:
    """從檢討文字抽出 `後續任務: ...` 行（供 autopilot 回寫 backlog）。"""
    return [m.strip() for m in re.findall(r"^\s*後續任務\s*[:：]\s*(.+)$", text, re.M)][:10]


def _build_experts(session_id: str, cwd: Path) -> dict[str, ExpertLike]:
    # 依設定的 provider 建立專家（延後 import，避免無 SDK 時就失敗）
    from .providers import make_expert

    return {role.key: make_expert(role, session_id, cwd) for role in ROSTER}


class StudioSession:
    def __init__(
        self,
        session_id: str,
        broadcast: Broadcast,
        experts: dict[str, ExpertLike] | None = None,
        cwd: Path | None = None,
        intervention_queue: asyncio.Queue[str] | None = None,
        repo_url: str | None = None,
    ):
        self.session_id = session_id
        self.broadcast = broadcast
        self.cwd = cwd
        self._experts = experts
        self._intervention = intervention_queue
        self._repo_url = repo_url  # 已 clone 進 workspace 的既有 GitHub repo（可選）
        self._tasks: list[dict] = []  # {id, title, status}
        self._run_command: str | None = None  # PM/工程師宣告的執行指令
        self._requirement = ""
        self._stop = False
        self._followups: list[str] = []  # 檢討時發現的後續任務（autopilot 回寫 backlog）
        self._last_commit: str | None = None  # 最近一次 workspace commit 短 hash

    # --- 控制 ----------------------------------------------------------
    def request_stop(self) -> None:
        self._stop = True

    def _drain_human(self) -> str:
        """取出所有待處理的人類插話，合併成一段文字（無則回空字串）。"""
        if self._intervention is None:
            return ""
        texts: list[str] = []
        while True:
            try:
                texts.append(self._intervention.get_nowait())
            except asyncio.QueueEmpty:
                break
        return "\n".join(t for t in texts if t.strip())

    async def _human_prefix(self) -> str:
        """取出插話、回顯到討論串，並組成要前綴給專家的字串。"""
        human = self._drain_human()
        if not human:
            return ""
        await self.broadcast(events.human_message(self.session_id, human))
        return f"【使用者插話，請納入考量】{human}\n\n"

    def _get_experts(self) -> dict[str, ExpertLike]:
        if self._experts is None:
            assert self.cwd is not None
            self._experts = _build_experts(self.session_id, self.cwd)
        return self._experts

    # --- 看板 ----------------------------------------------------------
    async def _board(self) -> None:
        """依各任務的 status 分欄，發看板更新事件。"""
        columns: dict[str, list[dict]] = {"todo": [], "doing": [], "review": [], "done": []}
        for t in self._tasks:
            columns.setdefault(t["status"], columns["todo"]).append({"title": t["title"]})
        await self.broadcast(events.board_update(self.session_id, columns))

    async def _set_task_status(self, task: dict, status: str) -> None:
        task["status"] = status
        await self.broadcast(events.task_status(self.session_id, task["id"], task["title"], status))
        await self._board()

    # --- git --------------------------------------------------------------
    async def _commit(self, message: str) -> None:
        if not self.cwd:
            return
        h = await runner.git_commit(self.cwd, message)
        if h:
            self._last_commit = h
            await self.broadcast(events.git_commit(self.session_id, message, h))

    # --- 辯論 ----------------------------------------------------------
    async def _debate(self, a: ExpertLike, b: ExpertLike, topic: str, rounds: int) -> None:
        """a 提案、b 點評、a 回應，來回 rounds 輪。rounds<=0 則跳過。"""
        if rounds <= 0 or self._stop:
            return
        await self.broadcast(
            events.phase_change(self.session_id, "架構討論", "工程師與高級工程師對齊做法")
        )
        proposal = await a.speak(
            f"{topic}\n請先簡短提出你打算採取的整體做法與檔案結構。", self.broadcast
        )
        for i in range(rounds):
            if self._stop:
                return
            critique = await b.speak(
                f"針對以下做法給出贊成點、疑慮與（必要時）替代方案，簡短：\n\n{proposal}",
                self.broadcast,
            )
            if i == rounds - 1:
                break
            proposal = await a.speak(
                f"針對以下意見回應並調整你的做法，簡短：\n\n{critique}", self.broadcast
            )

    async def _architecture_decision(
        self,
        architect: ExpertLike,
        engineer: ExpertLike,
        senior: ExpertLike,
        topic: str,
        research_notes: str,
    ) -> str:
        """架構師主導設計：提案 → 工程師/高級工程師給意見 → 架構師定案。回傳設計決策文字。"""
        await self.broadcast(
            events.phase_change(self.session_id, "架構決策", "架構師主導設計決策")
        )
        rnote = f"研究員調研供參考：\n{research_notes}\n\n" if research_notes else ""
        proposal = await architect.speak(
            rnote + topic + "\n\n請提出整體設計：技術選型、模組邊界、資料流與關鍵取捨。",
            self.broadcast,
        )
        eng_view = await engineer.speak(
            f"針對以下架構設計，從實作可行性給簡短意見：\n\n{proposal}", self.broadcast
        )
        senior_view = await senior.speak(
            f"針對以下架構設計，從品質/維護/風險給簡短意見：\n\n{proposal}", self.broadcast
        )
        decision = await architect.speak(
            "綜合以下意見定案，逐行輸出 `設計決策: <決策>`：\n\n"
            f"【工程師】{eng_view}\n\n【高級工程師】{senior_view}",
            self.broadcast,
        )
        return decision

    # --- 主流程 --------------------------------------------------------
    async def run(self, requirement: str) -> dict:
        """執行整場討論。回傳結果摘要供 autopilot 使用（前端走 broadcast，不需回傳值）。"""
        result = {"completed": False, "followups": [], "commit": None}
        try:
            result = await self._run(requirement)
        except Exception as exc:  # noqa: BLE001 — 任何錯誤都回報給前端而非崩潰
            await self.broadcast(events.error(self.session_id, f"{type(exc).__name__}: {exc}"))
        finally:
            for ex in (self._experts or {}).values():
                try:
                    await ex.stop()
                except Exception:  # noqa: BLE001
                    pass
        return result

    async def _run(self, requirement: str) -> None:
        self._requirement = requirement
        experts = self._get_experts()
        pm, engineer, qa, senior = (
            experts["pm"],
            experts["engineer"],
            experts["qa"],
            experts["senior"],
        )
        # 可選角色：不存在（offline 或被 TI_OPTIONAL_ROLES 關閉）就跳過對應階段。
        researcher = experts.get("researcher")
        architect = experts.get("architect")
        security = experts.get("security")
        devops = experts.get("devops")

        await self.broadcast(
            events.StudioEvent(
                events.EventType.SESSION_STARTED,
                self.session_id,
                {
                    "requirement": requirement,
                    "repo_url": self._repo_url,
                    # 以實際建立的專家為準（offline 顯示 4 位、正式顯示全部）。
                    "roster": [
                        {
                            "key": ex.role.key,
                            "name": ex.role.name,
                            "avatar": ex.role.avatar,
                            "title": ex.role.title,
                            "tags": ex.role.tags,
                        }
                        for ex in experts.values()
                    ],
                },
            )
        )
        if self.cwd:
            await runner.git_init(self.cwd)

        # 0) 調研（研究員上網查資料，供拆解與設計參考）
        research_notes = ""
        if researcher:
            await self.broadcast(
                events.phase_change(self.session_id, "調研", "研究員正在查資料")
            )
            research_notes = await researcher.speak(
                f"團隊即將開發以下需求，請先上網調研以提供決策依據：\n\n{requirement}\n\n"
                "查可用套件/函式庫、官方 API 與文件、最佳實踐與常見坑，精簡彙整並附來源。",
                self.broadcast,
            )

        # 1) 拆解
        await self.broadcast(events.phase_change(self.session_id, "需求拆解", "PM 正在拆解需求"))
        repo_note = (
            "我們要在一個現有的 GitHub 專案上工作，原始碼已 clone 到你的工作目錄"
            f"（{self._repo_url}）。請先用工具瀏覽現有結構與檔案，再依需求拆解任務。\n\n"
            if self._repo_url
            else ""
        )
        research_note = (
            f"研究員的調研結論供參考：\n{research_notes}\n\n" if research_notes else ""
        )
        pm_plan = await pm.speak(
            (await self._human_prefix())
            + repo_note
            + research_note
            + f"使用者的產品需求如下：\n\n{requirement}\n\n"
            "請拆解成結構化任務清單與驗收標準，並宣告執行指令。",
            self.broadcast,
        )
        self._run_command = runner.parse_run_command(pm_plan)
        self._tasks = [
            {"id": i, "title": t, "status": "todo"}
            for i, t in enumerate(parse_tasks(pm_plan), start=1)
        ]
        await self._board()
        await self._commit("PM 規劃：建立任務清單與驗收標準")

        # 2) 架構：有架構師則由其主導設計決策，否則維持工程師⇄高級工程師辯論
        design_note = ""
        topic = f"我們要實作這個需求：{requirement}\n任務清單：\n{pm_plan}"
        if architect:
            design_note = await self._architecture_decision(
                architect, engineer, senior, topic, research_notes
            )
        else:
            await self._debate(engineer, senior, topic=topic, rounds=config.DEBATE_ROUNDS)

        # 供每個任務實作時參考的脈絡（調研 + 設計決策）
        context = ""
        if research_notes:
            context += f"\n【研究員調研】\n{research_notes}\n"
        if design_note:
            context += f"\n【架構決策】\n{design_note}\n"

        # 3) 逐任務迭代
        all_ok = True
        for task in self._tasks:
            if self._stop:
                break
            await self.broadcast(
                events.phase_change(self.session_id, "實作", f"任務 #{task['id']}：{task['title']}")
            )
            await self._set_task_status(task, "doing")
            task_ok = await self._work_task(task, pm_plan + context, engineer, qa, senior, security)
            # 卡關升級：跑滿輪數仍未通過 → 召集 huddle 討論替代方案 + 給 1 輪重試。
            if not task_ok and config.HUDDLE_ENABLED and not self._stop:
                task_ok = await self._huddle_and_retry(
                    task, pm_plan + context, pm, architect, engineer, qa, senior, security
                )
            all_ok = all_ok and task_ok
            await self._set_task_status(task, "done" if task_ok else "review")

        # 3.5) 整合驗證（維運：裝相依、設環境、跑整合/啟動驗證）
        if devops:
            await self.broadcast(
                events.phase_change(self.session_id, "整合驗證", "維運工程師驗證整合與環境")
            )
            await devops.speak(
                "請確保整體成果能在乾淨環境跑起來：安裝相依、設定必要環境、實際啟動或跑整合測試，"
                f"並回報結果。整體計畫供參考：\n{pm_plan}",
                self.broadcast,
            )

        # 4) 最終 Demo（實際執行整體產出）
        await self._final_demo()

        # 5) PM 驗收 + 檢討
        done = await self._wrap_up(pm, all_ok)

        # 6) 視設定自動發佈成果到 GitHub
        await self._maybe_publish(done)

        return {"completed": done, "followups": self._followups, "commit": self._last_commit}

    async def _work_task(
        self,
        task: dict,
        pm_plan: str,
        engineer: ExpertLike,
        qa: ExpertLike,
        senior: ExpertLike,
        security: ExpertLike | None = None,
        *,
        max_rounds: int | None = None,
        seed_feedback: str = "",
    ) -> bool:
        """單一任務的 實作→自測→驗證→審查→改進 迴圈，回傳是否通過。

        max_rounds：限制本次迴圈輪數（huddle 後重試只給 1 輪）；None 用 config 預設。
        seed_feedback：預先注入的回饋（huddle 結論），非空時第一輪即走「改進」路徑。
        """
        feedback = seed_feedback
        rounds = max_rounds if max_rounds is not None else config.TASK_MAX_ROUNDS
        for rnd in range(1, rounds + 1):
            if self._stop:
                return False
            human = await self._human_prefix()

            # --- 實作 ---
            if not feedback:
                impl_prompt = (
                    f"{human}目前要完成的任務 #{task['id']}：{task['title']}\n\n"
                    f"整體計畫供參考：\n{pm_plan}\n\n"
                    "請在工作目錄裡實作，並在交付前自己跑過一次確認能執行。"
                )
            else:
                impl_prompt = (
                    f"{human}任務 #{task['id']}：{task['title']} 尚未通過，"
                    f"請根據以下意見逐項修正（第 {rnd} 輪）：\n\n{feedback}\n\n"
                    "修正後請自己再跑一次確認。"
                )
            impl_text = await engineer.speak(impl_prompt, self.broadcast)

            # --- 交付前自測（確定性 smoke-run）---
            await self._self_test(impl_text)
            await self._commit(f"任務#{task['id']} 第{rnd}輪：{task['title']}")

            # --- 驗證 ---
            await self.broadcast(
                events.phase_change(
                    self.session_id, "驗證", f"任務 #{task['id']} 驗證中（第 {rnd} 輪）"
                )
            )
            qa_text = await qa.speak(
                f"請針對任務 #{task['id']}：{task['title']} 的程式碼撰寫並執行測試，"
                f"驗證是否符合驗收標準：\n\n{pm_plan}",
                self.broadcast,
            )
            qa_ok = qa_passed(qa_text)
            await self.broadcast(
                events.run_result(self.session_id, qa_ok, "驗證通過" if qa_ok else "驗證未通過")
            )

            # --- 審查（帶入 QA 測試結果）---
            await self.broadcast(
                events.phase_change(
                    self.session_id, "審查", f"任務 #{task['id']} 審查中（第 {rnd} 輪）"
                )
            )
            await self._set_task_status(task, "review")
            senior_text = await senior.speak(
                f"請審查任務 #{task['id']}：{task['title']} 的程式碼（品質、設計、安全），並給出決議。\n\n"
                f"驗證工程師的測試結果如下，請納入判斷：\n{qa_text}",
                self.broadcast,
            )
            senior_ok = senior_approved(senior_text)

            # --- 資安審查（有資安審查員時，為通過的必要條件）---
            sec_text = ""
            security_ok = True
            if security:
                await self.broadcast(
                    events.phase_change(
                        self.session_id, "資安審查", f"任務 #{task['id']} 資安把關（第 {rnd} 輪）"
                    )
                )
                sec_text = await security.speak(
                    f"請對任務 #{task['id']}：{task['title']} 的程式碼做資安審查，"
                    "輸出 `決議: 安全核可` 或 `決議: 安全退回`（退回時列具體風險）。",
                    self.broadcast,
                )
                security_ok = security_approved(sec_text)

            if qa_ok and senior_ok and security_ok:
                return True

            # --- 帶意見回饋，準備下一輪 ---
            feedback = f"【驗證工程師回報】\n{qa_text}\n\n【高級工程師審查意見】\n{senior_text}"
            if sec_text:
                feedback += f"\n\n【資安審查意見】\n{sec_text}"
            await self.broadcast(
                events.phase_change(
                    self.session_id,
                    "改進討論",
                    f"任務 #{task['id']} 第 {rnd} 輪未通過，工程師將依意見修正",
                )
            )
        return False

    async def _huddle_and_retry(
        self,
        task: dict,
        context: str,
        pm: ExpertLike,
        architect: ExpertLike | None,
        engineer: ExpertLike,
        qa: ExpertLike,
        senior: ExpertLike,
        security: ExpertLike | None,
    ) -> bool:
        """卡關升級：召集團隊 huddle 找替代方案 → 給 1 輪重試。

        重試仍失敗則把 task 標為「已知限制」（註記 + 事件），status 由呼叫端維持 review。
        """
        conclusion = await self._huddle(task, context, pm, architect, engineer, senior)
        task_ok = await self._work_task(
            task,
            context,
            engineer,
            qa,
            senior,
            security,
            max_rounds=1,
            seed_feedback=f"【卡關 huddle 替代方案，請據此突破】\n{conclusion}",
        )
        if not task_ok:
            task["limitation"] = True
            await self.broadcast(
                events.huddle(
                    self.session_id,
                    task["id"],
                    task["title"],
                    [],
                    "huddle 與重試後仍未通過，標記為『已知限制』，不靜默帶過。",
                    limitation=True,
                )
            )
        return task_ok

    async def _huddle(
        self,
        task: dict,
        context: str,
        pm: ExpertLike,
        architect: ExpertLike | None,
        engineer: ExpertLike,
        senior: ExpertLike,
    ) -> str:
        """召集卡關討論：依序讓在場角色針對 blocker 提替代方案。回傳彙整結論。

        召集 PM＋架構師＋工程師＋高級工程師，缺席角色（如 offline 無架構師）自動略過。
        """
        roster = [("pm", pm), ("architect", architect), ("engineer", engineer), ("senior", senior)]
        present = [(key, ex) for key, ex in roster if ex is not None]
        await self.broadcast(
            events.phase_change(
                self.session_id,
                "卡關討論",
                f"任務 #{task['id']} 連續失敗，召集團隊討論替代方案",
            )
        )
        blocker = (
            f"任務 #{task['id']}：{task['title']} 連續 {config.TASK_MAX_ROUNDS} 輪未通過，卡關了。\n"
            f"整體計畫供參考：\n{context}\n\n"
        )
        notes: list[str] = []
        for _key, ex in present:
            prior = ("\n團隊目前的討論：\n" + "\n".join(notes)) if notes else ""
            view = await ex.speak(
                blocker
                + "請針對這個 blocker 提出可突破的替代做法或拆解方式，簡短具體、可立即執行。"
                + prior,
                self.broadcast,
            )
            notes.append(f"【{ex.role.name}】{view}")
        conclusion = "\n".join(notes)
        await self.broadcast(
            events.huddle(
                self.session_id,
                task["id"],
                task["title"],
                [key for key, _ in present],
                conclusion,
            )
        )
        return conclusion

    async def _self_test(self, impl_text: str) -> None:
        """工程師交付前的確定性 smoke-run，把完整 log 回報。"""
        if not self.cwd:
            return
        cmd = runner.parse_run_command(impl_text) or runner.resolve_demo_command(
            self.cwd, self._run_command
        )
        if not cmd:
            return
        result = await runner.run_command(self.cwd, cmd)
        await self.broadcast(
            events.run_result(
                self.session_id,
                result.ok,
                f"自測 `{cmd}`：{'通過' if result.ok else '未通過'}",
                log=result.output,
            )
        )

    async def _final_demo(self) -> None:
        if not self.cwd or self._stop:
            return
        cmd = runner.resolve_demo_command(self.cwd, self._run_command)
        if not cmd:
            return
        await self.broadcast(events.phase_change(self.session_id, "Demo", "實際執行成果"))
        result = await runner.run_command(self.cwd, cmd)
        await self.broadcast(
            events.demo_result(self.session_id, cmd, result.exit_code, result.output, label="Demo")
        )

    async def _wrap_up(self, pm: ExpertLike, all_ok: bool) -> bool:
        await self.broadcast(events.phase_change(self.session_id, "驗收", "PM 確認驗收標準"))
        verdict = await pm.speak(
            (await self._human_prefix()) + "請依驗收標準檢查目前工作目錄的成果，判斷是否完成"
            "（輸出 `決議: 完成` 或 `決議: 未完成`）。",
            self.broadcast,
        )
        done = pm_done(verdict) and all_ok and not self._stop

        await self.broadcast(events.phase_change(self.session_id, "檢討", "團隊進行回顧"))
        retro = await pm.speak(
            "請帶領團隊做一段簡短檢討：這次做得好的地方、可以改進的地方、以及後續建議。\n"
            "若過程中發現尚未解決的問題或值得改善之處，請在最後逐行列出後續任務，"
            "每行格式固定為 `後續任務: <動詞開頭的具體任務>`（沒有就不必列）。",
            self.broadcast,
        )
        self._followups = parse_followups(retro)
        await self.broadcast(
            events.StudioEvent(events.EventType.RETROSPECTIVE, self.session_id, {"text": retro})
        )
        await self._commit("完成：交付成果與檢討")

        files = workspace.list_files(self.session_id) if self.cwd else []
        await self.broadcast(
            events.StudioEvent(
                events.EventType.DONE,
                self.session_id,
                {"completed": done, "stopped": self._stop, "files": files},
            )
        )
        return done

    async def _maybe_publish(self, done: bool) -> None:
        """專案完成且設定允許時，自動把成果發佈到 GitHub。"""
        if not self.cwd or self._stop or not done:
            return
        if not (config.PUBLISH_AUTO and publisher.is_configured()):
            return
        await self.broadcast(events.phase_change(self.session_id, "發佈", "推送成果到 GitHub"))
        result = await publisher.publish(self.cwd, self.session_id, self._requirement)
        await self.broadcast(events.publish_result(self.session_id, result.to_dict()))
