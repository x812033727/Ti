"""StudioSession — 工作室的討論/工作流程狀態機（核心）。

Phase 2 流程：PM 拆解結構化任務 → 架構辯論（工程師⇄高級工程師）→ 逐任務迭代
（實作→交付前自測→驗證→審查→帶意見改進，每任務最多 TASK_MAX_ROUNDS 輪）→ 最終實際 Demo
→ PM 驗收 → 團隊檢討。支援人類中途插話與停止。每一步都透過 broadcast callback 送事件。

為了可測試，experts 以 dict 注入；確定性執行（跑程式 / git）集中在 runner，cwd=None 時跳過。
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Awaitable, Callable, Protocol

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
        intervention_queue: "asyncio.Queue[str] | None" = None,
    ):
        self.session_id = session_id
        self.broadcast = broadcast
        self.cwd = cwd
        self._experts = experts
        self._intervention = intervention_queue
        self._tasks: list[dict] = []          # {id, title, status}
        self._run_command: str | None = None  # PM/工程師宣告的執行指令
        self._requirement = ""
        self._stop = False

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
        await self.broadcast(
            events.task_status(self.session_id, task["id"], task["title"], status)
        )
        await self._board()

    # --- git --------------------------------------------------------------
    async def _commit(self, message: str) -> None:
        if not self.cwd:
            return
        h = await runner.git_commit(self.cwd, message)
        if h:
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

    # --- 主流程 --------------------------------------------------------
    async def run(self, requirement: str) -> None:
        try:
            await self._run(requirement)
        except Exception as exc:  # noqa: BLE001 — 任何錯誤都回報給前端而非崩潰
            await self.broadcast(events.error(self.session_id, f"{type(exc).__name__}: {exc}"))
        finally:
            for ex in (self._experts or {}).values():
                try:
                    await ex.stop()
                except Exception:  # noqa: BLE001
                    pass

    async def _run(self, requirement: str) -> None:
        self._requirement = requirement
        experts = self._get_experts()
        pm, engineer, qa, senior = (
            experts["pm"], experts["engineer"], experts["qa"], experts["senior"]
        )

        await self.broadcast(
            events.StudioEvent(
                events.EventType.SESSION_STARTED,
                self.session_id,
                {"requirement": requirement, "roster": [
                    {"key": r.key, "name": r.name, "avatar": r.avatar,
                     "title": r.title, "tags": r.tags}
                    for r in ROSTER
                ]},
            )
        )
        if self.cwd:
            await runner.git_init(self.cwd)

        # 1) 拆解
        await self.broadcast(events.phase_change(self.session_id, "需求拆解", "PM 正在拆解需求"))
        pm_plan = await pm.speak(
            (await self._human_prefix())
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

        # 2) 架構辯論
        await self._debate(
            engineer, senior,
            topic=f"我們要實作這個需求：{requirement}\n任務清單：\n{pm_plan}",
            rounds=config.DEBATE_ROUNDS,
        )

        # 3) 逐任務迭代
        all_ok = True
        for task in self._tasks:
            if self._stop:
                break
            await self.broadcast(
                events.phase_change(self.session_id, "實作", f"任務 #{task['id']}：{task['title']}")
            )
            await self._set_task_status(task, "doing")
            task_ok = await self._work_task(task, pm_plan, engineer, qa, senior)
            all_ok = all_ok and task_ok
            await self._set_task_status(task, "done" if task_ok else "review")

        # 4) 最終 Demo（實際執行整體產出）
        await self._final_demo()

        # 5) PM 驗收 + 檢討
        done = await self._wrap_up(pm, all_ok)

        # 6) 視設定自動發佈成果到 GitHub
        await self._maybe_publish(done)

    async def _work_task(
        self, task: dict, pm_plan: str,
        engineer: ExpertLike, qa: ExpertLike, senior: ExpertLike,
    ) -> bool:
        """單一任務的 實作→自測→驗證→審查→改進 迴圈，回傳是否通過。"""
        feedback = ""
        for rnd in range(1, config.TASK_MAX_ROUNDS + 1):
            if self._stop:
                return False
            human = await self._human_prefix()

            # --- 實作 ---
            if rnd == 1:
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
                events.phase_change(self.session_id, "驗證", f"任務 #{task['id']} 驗證中（第 {rnd} 輪）")
            )
            qa_text = await qa.speak(
                f"請針對任務 #{task['id']}：{task['title']} 的程式碼撰寫並執行測試，"
                f"驗證是否符合驗收標準：\n\n{pm_plan}",
                self.broadcast,
            )
            qa_ok = qa_passed(qa_text)
            await self.broadcast(
                events.run_result(
                    self.session_id, qa_ok, "驗證通過" if qa_ok else "驗證未通過"
                )
            )

            # --- 審查（帶入 QA 測試結果）---
            await self.broadcast(
                events.phase_change(self.session_id, "審查", f"任務 #{task['id']} 審查中（第 {rnd} 輪）")
            )
            await self._set_task_status(task, "review")
            senior_text = await senior.speak(
                f"請審查任務 #{task['id']}：{task['title']} 的程式碼（品質、設計、安全），並給出決議。\n\n"
                f"驗證工程師的測試結果如下，請納入判斷：\n{qa_text}",
                self.broadcast,
            )
            senior_ok = senior_approved(senior_text)

            if qa_ok and senior_ok:
                return True

            # --- 帶意見回饋，準備下一輪 ---
            feedback = (
                f"【驗證工程師回報】\n{qa_text}\n\n"
                f"【高級工程師審查意見】\n{senior_text}"
            )
            await self.broadcast(
                events.phase_change(
                    self.session_id, "改進討論",
                    f"任務 #{task['id']} 第 {rnd} 輪未通過，工程師將依意見修正",
                )
            )
        return False

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
            events.demo_result(
                self.session_id, cmd, result.exit_code, result.output, label="Demo"
            )
        )

    async def _wrap_up(self, pm: ExpertLike, all_ok: bool) -> bool:
        await self.broadcast(events.phase_change(self.session_id, "驗收", "PM 確認驗收標準"))
        verdict = await pm.speak(
            (await self._human_prefix())
            + "請依驗收標準檢查目前工作目錄的成果，判斷是否完成"
            "（輸出 `決議: 完成` 或 `決議: 未完成`）。",
            self.broadcast,
        )
        done = pm_done(verdict) and all_ok and not self._stop

        await self.broadcast(events.phase_change(self.session_id, "檢討", "團隊進行回顧"))
        retro = await pm.speak(
            "請帶領團隊做一段簡短檢討：這次做得好的地方、可以改進的地方、以及後續建議。",
            self.broadcast,
        )
        await self.broadcast(
            events.StudioEvent(
                events.EventType.RETROSPECTIVE, self.session_id, {"text": retro}
            )
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
