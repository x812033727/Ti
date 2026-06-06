"""StudioSession — 工作室的討論/工作流程狀態機（核心）。

流程：PM 拆解 → 工程師實作 → 驗證工程師測試 → 高級工程師審查 →（失敗/退回則回到工程師，
最多 MAX_ROUNDS 輪）→ PM 判斷完成並帶領檢討。每一步都透過 broadcast callback 把事件送出。

為了可測試，experts 以 dict 注入；單元測試可塞入 stub 專家，不需呼叫真正的 LLM。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Awaitable, Callable, Protocol

from . import config, events, workspace
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
    """從 PM 的拆解文字抽出任務條目，給看板用。"""
    tasks: list[str] = []
    for line in pm_text.splitlines():
        m = re.match(r"^\s*(?:[-*•]|\d+[.)、])\s+(.*)$", line)
        if m:
            item = m.group(1).strip()
            # 略過明顯是『驗收標準』段落的條目，盡量只留任務
            if item and len(item) < 200:
                tasks.append(item)
    return tasks[:12] or ["實作需求"]


def _build_experts(session_id: str, cwd: Path) -> dict[str, ExpertLike]:
    # 延後 import，避免單元測試在沒裝 SDK 時就失敗
    from .experts import Expert

    return {role.key: Expert(role, session_id, cwd) for role in ROSTER}


class StudioSession:
    def __init__(
        self,
        session_id: str,
        broadcast: Broadcast,
        experts: dict[str, ExpertLike] | None = None,
        cwd: Path | None = None,
    ):
        self.session_id = session_id
        self.broadcast = broadcast
        self.cwd = cwd
        self._experts = experts
        self._tasks: list[str] = []

    def _get_experts(self) -> dict[str, ExpertLike]:
        if self._experts is None:
            assert self.cwd is not None
            self._experts = _build_experts(self.session_id, self.cwd)
        return self._experts

    async def _board(self, column_of_all: str) -> None:
        """把所有任務集中放在某一欄，發看板更新事件。"""
        columns = {"todo": [], "doing": [], "review": [], "done": []}
        columns[column_of_all] = [{"title": t} for t in self._tasks]
        await self.broadcast(events.board_update(self.session_id, columns))

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
        experts = self._get_experts()
        pm = experts["pm"]
        engineer = experts["engineer"]
        qa = experts["qa"]
        senior = experts["senior"]

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

        # 1) 拆解
        await self.broadcast(events.phase_change(self.session_id, "需求拆解", "PM 正在拆解需求"))
        pm_plan = await pm.speak(
            f"使用者的產品需求如下：\n\n{requirement}\n\n"
            "請拆解成任務清單與驗收標準。",
            self.broadcast,
        )
        self._tasks = parse_tasks(pm_plan)
        await self._board("todo")

        approved = False
        for rnd in range(1, config.MAX_ROUNDS + 1):
            await self.broadcast(
                events.phase_change(self.session_id, "實作", f"第 {rnd} 輪 — 工程師開發中")
            )
            await self._board("doing")

            impl_prompt = (
                f"請依以下計畫實作（這是第 {rnd} 輪）：\n\n{pm_plan}\n\n"
                "在工作目錄裡寫出可運行的程式碼。"
                if rnd == 1
                else (
                    "請根據上一輪的驗證/審查意見修正你的程式碼，並說明你改了什麼。"
                )
            )
            await engineer.speak(impl_prompt, self.broadcast)

            # 3) 驗證
            await self.broadcast(
                events.phase_change(self.session_id, "驗證", f"第 {rnd} 輪 — 驗證工程師測試中")
            )
            qa_text = await qa.speak(
                "請針對目前工作目錄裡的程式碼撰寫並執行測試，驗證是否符合驗收標準：\n\n"
                f"{pm_plan}",
                self.broadcast,
            )
            qa_ok = qa_passed(qa_text)
            await self.broadcast(
                events.run_result(self.session_id, qa_ok, "驗證通過" if qa_ok else "驗證未通過")
            )

            # 4) 審查
            await self.broadcast(
                events.phase_change(self.session_id, "審查", f"第 {rnd} 輪 — 高級工程師審查中")
            )
            await self._board("review")
            senior_text = await senior.speak(
                "請審查目前工作目錄裡的程式碼（品質、設計、安全），並給出決議。"
                + ("\n注意：驗證工程師回報測試未全部通過。" if not qa_ok else ""),
                self.broadcast,
            )
            senior_ok = senior_approved(senior_text)

            if qa_ok and senior_ok:
                approved = True
                break

            # 5) 退回，準備下一輪
            await self.broadcast(
                events.phase_change(
                    self.session_id, "改進討論",
                    f"第 {rnd} 輪未通過，工程師將依意見修正",
                )
            )

        # 6) PM 判斷完成 + 檢討
        await self.broadcast(events.phase_change(self.session_id, "驗收", "PM 確認驗收標準"))
        verdict = await pm.speak(
            "請依驗收標準檢查目前工作目錄的成果，判斷是否完成（輸出 `決議: 完成` 或 `決議: 未完成`）。",
            self.broadcast,
        )
        done = pm_done(verdict) and approved
        await self._board("done" if done else "review")

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

        files = workspace.list_files(self.session_id) if self.cwd else []
        await self.broadcast(
            events.StudioEvent(
                events.EventType.DONE,
                self.session_id,
                {"completed": done, "files": files},
            )
        )
