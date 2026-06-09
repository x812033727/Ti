"""StudioSession — 工作室的討論/工作流程狀態機（核心）。

Phase 2 流程：PM 拆解結構化任務 → 架構辯論（工程師⇄高級工程師）→ 逐任務迭代
（實作→交付前自測→驗證→審查→帶意見改進，每任務最多 TASK_MAX_ROUNDS 輪）→ 最終實際 Demo
→ PM 驗收 → 團隊檢討。支援人類中途插話與停止。每一步都透過 broadcast callback 送事件。

為了可測試，experts 以 dict 注入；確定性執行（跑程式 / git）集中在 runner，cwd=None 時跳過。
"""

from __future__ import annotations

import asyncio
import difflib
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from . import config, events, publisher, runner, workspace
from .roles import ROSTER, Role

Broadcast = Callable[[events.StudioEvent], Awaitable[None]]


class ExpertLike(Protocol):
    role: Role

    async def speak(self, prompt: str, broadcast: Broadcast) -> str: ...
    async def stop(self) -> None: ...


@dataclass
class LaneContext:
    """單一執行支線（lane）的隔離狀態。

    循序模式只有一條 "main" lane：cwd/experts 即 session 本身、branch=None。並行模式下每條
    lane 各有獨立的 worktree 目錄、專家團隊與 last_commit，彼此不干擾。NOTES 在 lane 內先寫進
    notes_buffer，由排程器在波次結束時序列化 flush 進共享 NOTES.md，避免並行寫檔競態。
    """

    lane_id: str
    cwd: Path | None
    experts: dict[str, ExpertLike]
    critics: dict[str, ExpertLike] | None = None
    branch: str | None = None
    last_commit: str | None = None
    notes_buffer: list[str] = field(default_factory=list)


@dataclass
class LaneResult:
    """一條 lane 跑完一波內配給任務後的結果，供波次收尾（合併/flush/清理）使用。"""

    ctx: LaneContext
    tasks: list[dict]
    ok: bool


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


def critic_blocks(text: str) -> bool:
    """異議檢查判定：critic 是否提出『成立』的異議（True=需退回，False=放行）。"""
    verdict = _last_match(text, r"異議\s*[:：]\s*(成立|不成立)")
    if verdict:
        return verdict == "成立"
    # 後備：無標記時偏向放行，僅在出現明確反對字樣時才退回，避免誤擋。
    return bool(re.search(r"(異議成立|不應通過|尚未完成|還不算完成)", text))


def text_similarity(a: str, b: str) -> float:
    """兩段文字的相似度（0~1）。用於偵測『只是重述、無實質進展』。"""
    a, b = a.strip(), b.strip()
    if not a and not b:
        return 1.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def is_stalled(history: list[str], rounds: int, threshold: float = 0.9) -> bool:
    """最近 rounds 筆發言彼此高度相似（無實質進展）即視為停滯。

    rounds<=1 或歷史不足 rounds 筆時不判定停滯（避免一開始就誤觸）。
    """
    if rounds <= 1 or len(history) < rounds:
        return False
    recent = history[-rounds:]
    first = recent[0]
    return all(text_similarity(first, t) >= threshold for t in recent[1:])


def pm_done(text: str) -> bool:
    verdict = _last_match(text, r"決議\s*[:：]\s*(完成|未完成)")
    if verdict:
        return verdict == "完成"
    return bool(re.search(r"(已完成|達成|符合驗收)", text))


def parse_tasks(pm_text: str) -> list[str]:
    """從 PM 的拆解文字抽出任務條目。優先 `任務: ...`，否則退回條列項目。"""
    cap = config.MAX_TASKS
    explicit = [m.strip() for m in re.findall(r"^\s*任務\s*[:：]\s*(.+)$", pm_text, re.M)]
    if explicit:
        return explicit[:cap]
    tasks: list[str] = []
    for line in pm_text.splitlines():
        m = re.match(r"^\s*(?:[-*•]|\d+[.)、])\s+(.*)$", line)
        if m:
            item = m.group(1).strip()
            if item and len(item) < 200 and not re.search(r"(執行指令|執行命令)", item):
                tasks.append(item)
    return tasks[:cap] or ["實作需求"]


def parse_followups(text: str) -> list[str]:
    """從檢討文字抽出 `後續任務: ...` 行（供 autopilot 回寫 backlog）。"""
    return [m.strip() for m in re.findall(r"^\s*後續任務\s*[:：]\s*(.+)$", text, re.M)][:10]


def parse_tasks_with_deps(pm_text: str) -> tuple[list[dict], list[tuple[int, int]]]:
    """從 PM 拆解文字抽出任務（含可選 `#id`）與依賴邊，供並行分波使用。

    任務行：`任務: [#<id>] <title>`（`#id` 可選，缺則依出現序自動編號，1-based）。
    依賴行：`依賴: #<after> -> #<before>`（after 須在 before 完成後才做）。
    無顯式 `任務:` 行時退回 `parse_tasks` 的條列解析（自動編號、無依賴），與循序行為一致。
    指向不存在任務 id 的依賴邊一律丟棄（防懸空）。任務數沿用 `MAX_TASKS` 上限。
    """
    cap = config.MAX_TASKS
    tasks: list[dict] = []
    explicit = re.findall(r"^\s*任務\s*[:：]\s*(?:#(\d+)\s+)?(.+?)\s*$", pm_text, re.M)
    if explicit:
        used: set[int] = set()
        for pos, (rid, title) in enumerate(explicit[:cap], start=1):
            tid = int(rid) if rid else pos
            while tid in used:  # 顯式 id 與自動序衝突時往後讓位，保證 id 唯一。
                tid = max(used) + 1
            used.add(tid)
            tasks.append({"id": tid, "title": title.strip(), "status": "todo"})
    else:
        for pos, title in enumerate(parse_tasks(pm_text)[:cap], start=1):
            tasks.append({"id": pos, "title": title, "status": "todo"})

    valid_ids = {t["id"] for t in tasks}
    edges: list[tuple[int, int]] = []
    for after, before in re.findall(r"^\s*依賴\s*[:：]\s*#(\d+)\s*->\s*#(\d+)\s*$", pm_text, re.M):
        a, b = int(after), int(before)
        if a in valid_ids and b in valid_ids and a != b:
            edges.append((a, b))
    return tasks, edges


def build_waves(tasks: list[dict], edges: list[tuple[int, int]]) -> list[list[dict]]:
    """依依賴邊把任務拓撲分層成「波次」：同一波內任務彼此獨立、可並行；波次之間循序。

    邊 (after, before) 表示 after 須在 before 完成後才做。以 Kahn 演算法逐層取出入度 0 的
    任務（穩定按 id 排序，結果可重現）。偵測到循環依賴時，剩餘任務退回「每任務一波」的純
    循序 fallback，確保永遠有解、不卡死。指向未知 id 的邊忽略（防懸空）。
    """
    by_id = {t["id"]: t for t in tasks}
    indeg = {tid: 0 for tid in by_id}
    adj: dict[int, list[int]] = {tid: [] for tid in by_id}
    for after, before in edges:
        if after in by_id and before in by_id and after != before:
            adj[before].append(after)
            indeg[after] += 1

    waves: list[list[dict]] = []
    remaining = set(by_id)
    while remaining:
        layer = sorted(tid for tid in remaining if indeg[tid] == 0)
        if not layer:
            # 循環依賴：剩餘任務退回每任務一波（按 id 序），保證收斂、不靜默卡死。
            for tid in sorted(remaining):
                waves.append([by_id[tid]])
            break
        waves.append([by_id[tid] for tid in layer])
        for tid in layer:
            remaining.discard(tid)
            for nxt in adj[tid]:
                indeg[nxt] -= 1
    return waves


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
        critics: dict[str, ExpertLike] | None = None,
    ):
        self.session_id = session_id
        self.broadcast = broadcast
        self.cwd = cwd
        self._experts = experts
        # 異議檢查用的獨立 expert 實例（不與主 experts 共用對話/calls 序號）。
        self._critics = critics
        self._intervention = intervention_queue
        self._repo_url = repo_url  # 已 clone 進 workspace 的既有 GitHub repo（可選）
        self._tasks: list[dict] = []  # {id, title, status}
        self._edges: list[tuple[int, int]] = []  # 任務依賴邊 (after, before)，並行分波用
        # 全域 LLM 並發節流（lazy 建立，綁當前 event loop）；多 lane × 多 reviewer 時生效。
        self._llm_sem: asyncio.Semaphore | None = None
        # 並行 lane 的專家工廠（測試可注入 stub）；None 時用 providers.make_expert。
        self._lane_expert_factory = None
        self._run_command: str | None = None  # PM/工程師宣告的執行指令
        self._requirement = ""
        self._stop = False
        self._followups: list[str] = []  # 檢討時發現的後續任務（autopilot 回寫 backlog）
        self._last_commit: str | None = None  # 最近一次主分支 workspace commit 短 hash
        # 主（循序）lane 的隔離狀態；於 _run 建立後，所有對主 workspace 的操作都走它。
        self._main_ctx: LaneContext | None = None
        # 所有建立過的 lane（含 main），供 run() 結束時統一回收專家、避免子程序洩漏。
        self._lane_ctxs: list[LaneContext] = []

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

    # --- 異議檢查（critic）-------------------------------------------------
    def _get_critic(self, ctx: LaneContext, role_key: str) -> ExpertLike | None:
        """取得指定視角的獨立 critic expert（綁定到傳入 lane 的 cwd/critics）。

        優先用該 lane 已注入/建立的 critics（測試/離線）；否則在有 cwd 時以獨立 session 建一個
        新實例，確保不污染該 lane 主 experts 的對話與 calls 序號。都無法取得時回 None（放行）。
        """
        if ctx.critics is not None:
            return ctx.critics.get(role_key)
        # 離線示範未注入 critics 時不走真 provider（無金鑰），直接放行不報錯。
        if ctx.cwd is None or config.OFFLINE_MODE:
            return None
        from .providers import make_expert
        from .roles import BY_KEY

        critic = make_expert(BY_KEY[role_key], f"{self.session_id}:critic:{role_key}", ctx.cwd)
        ctx.critics = {role_key: critic}
        return critic

    async def _critic_gate(
        self, ctx: LaneContext, role_key: str, subject: str, acceptance: str
    ) -> tuple[bool, str]:
        """放行前的異議關卡。回傳 (是否放行, critic 文字)。

        刻意只餵標的與驗收標準、不餵當事人剛才的核可理由以降低錨定；停用或無 critic 時放行。
        """
        # 離線示範（OFFLINE_MODE）視為 demo 情境自動啟用，以展示「內部討論」事件。
        if not (config.CRITIC_ENABLED or config.OFFLINE_MODE) or self._stop:
            return True, ""
        critic = self._get_critic(ctx, role_key)
        if critic is None:
            return True, ""
        async with self._llm_semaphore():
            text = await critic.speak(
                "你是獨立的異議檢查者，專挑『為何這還不算完成』，以防團隊形成錯誤共識。\n"
                f"檢查標的：{subject}\n\n驗收標準：\n{acceptance}\n\n"
                "請只根據標的與驗收標準判斷，提出具體、實質的反對；找不到實質問題就放行。\n"
                "最後一行明確輸出：`異議: 成立`（需退回）或 `異議: 不成立`（放行）。",
                self.broadcast,
            )
        blocks = critic_blocks(text)
        await self.broadcast(events.critic_review(self.session_id, role_key, not blocks, text))
        return (not blocks), text

    # --- 共用知識庫（NOTES.md）----------------------------------------
    def _note(self, ctx: LaneContext, text: str) -> None:
        """把一段跨任務知識暫存到 lane 的 notes_buffer（停用或無 cwd 時略過）。

        刻意不立即寫檔：循序模式每任務結束 flush、並行模式每波次結束序列化 flush，
        以根除多 lane 同時 append NOTES.md 的競態。
        """
        if config.NOTES_ENABLED and ctx.cwd:
            text = (text or "").strip()
            if text:
                ctx.notes_buffer.append(text)

    def _flush_lane_notes(self, ctx: LaneContext) -> None:
        """把 lane 暫存的 notes_buffer 依序寫進共享 NOTES.md（單一寫入點，無競態），並清空。"""
        if not (config.NOTES_ENABLED and ctx.cwd):
            ctx.notes_buffer.clear()
            return
        for note in ctx.notes_buffer:
            workspace.append_note(self.session_id, note)
        ctx.notes_buffer.clear()

    def _notes_context(self, ctx: LaneContext) -> str:
        """讀回 NOTES.md，組成要注入實作 prompt 的前綴（停用/空白時回空字串）。"""
        if not (config.NOTES_ENABLED and ctx.cwd):
            return ""
        notes = workspace.read_notes(self.session_id)
        if not notes.strip():
            return ""
        return f"【團隊共用知識庫 NOTES.md（過往踩過的坑／決策／後續）】\n{notes}\n\n"

    # --- 停滯守門 ------------------------------------------------------
    def _stalled(self, ctx: LaneContext, history: list[str], committed_change: bool) -> bool:
        """是否陷入停滯（連續多輪只重述且無實質檔案變動）。

        無 cwd 或關閉 git 時一律回 False（保護 cwd=None 的單元測試不被提早收斂）；
        本輪有實質 commit 變動則視為有進展、不算停滯。文字相似度為主訊號。
        """
        if not ctx.cwd or not config.ENABLE_GIT:
            return False
        if config.STALL_ROUNDS <= 1 or committed_change:
            return False
        return is_stalled(history, config.STALL_ROUNDS)

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
    async def _commit(self, ctx: LaneContext, message: str) -> None:
        if not ctx.cwd:
            return
        h = await runner.git_commit(ctx.cwd, message)
        if h:
            ctx.last_commit = h
            # 主分支（branch=None）的 commit 同步到 session 級欄位（發佈/回傳值仍用它）。
            # 並行 lane 的 commit 不動 self._last_commit，改由波次合併後以主分支 HEAD 更新。
            if ctx.branch is None:
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
        await self.broadcast(events.phase_change(self.session_id, "架構決策", "架構師主導設計決策"))
        rnote = f"研究員調研供參考：\n{research_notes}\n\n" if research_notes else ""
        proposal = await architect.speak(
            rnote + topic + "\n\n請提出整體設計：技術選型、模組邊界、資料流與關鍵取捨。",
            self.broadcast,
        )
        # 工程師與高級工程師對同一份提案各自給意見，互相獨立 → 並行以省時。
        eng_view, senior_view = await asyncio.gather(
            engineer.speak(
                f"針對以下架構設計，從實作可行性給簡短意見：\n\n{proposal}", self.broadcast
            ),
            senior.speak(
                f"針對以下架構設計，從品質/維護/風險給簡短意見：\n\n{proposal}", self.broadcast
            ),
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
            # 回收所有 lane（含 main）的專家與 critic；安全網涵蓋 main_ctx 尚未建立但
            # experts 已 build 的情況。以實例身分去重（同一實例可能同時在數處被引用）。
            to_stop: list[ExpertLike] = []
            for ctx in self._lane_ctxs:
                to_stop += list(ctx.experts.values())
                to_stop += list((ctx.critics or {}).values())
            to_stop += list((self._experts or {}).values())
            to_stop += list((self._critics or {}).values())
            for ex in dict.fromkeys(to_stop):
                try:
                    await ex.stop()
                except Exception:  # noqa: BLE001
                    pass
        return result

    async def _run(self, requirement: str) -> None:
        self._requirement = requirement
        experts = self._get_experts()
        # 主（循序）lane：cwd/experts 即 session 本身。逐任務迭代與其 helper 全走它，
        # 行為與重構前逐字等價；並行模式（後續階段）才會另建隔離 lane。
        self._main_ctx = LaneContext(
            "main", self.cwd, experts, self._critics, last_commit=self._last_commit
        )
        self._lane_ctxs.append(self._main_ctx)
        # 架構/辯論/驗收/發佈等「整體階段」直接用到的角色（任務內角色由 lane.experts 提供）。
        pm, engineer, senior = experts["pm"], experts["engineer"], experts["senior"]
        # 可選角色：不存在（offline 或被 TI_OPTIONAL_ROLES 關閉）就跳過對應階段。
        researcher = experts.get("researcher")
        architect = experts.get("architect")
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
            await self.broadcast(events.phase_change(self.session_id, "調研", "研究員正在查資料"))
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
        research_note = f"研究員的調研結論供參考：\n{research_notes}\n\n" if research_notes else ""
        pm_plan = await pm.speak(
            (await self._human_prefix())
            + repo_note
            + research_note
            + f"使用者的產品需求如下：\n\n{requirement}\n\n"
            "請拆解成結構化任務清單與驗收標準，並宣告執行指令。",
            self.broadcast,
        )
        self._run_command = runner.parse_run_command(pm_plan)
        if config.PARALLEL_TASKS_ENABLED:
            # 並行：解析任務 + 依賴邊，供拓撲分波。
            self._tasks, self._edges = parse_tasks_with_deps(pm_plan)
        else:
            self._tasks = [
                {"id": i, "title": t, "status": "todo"}
                for i, t in enumerate(parse_tasks(pm_plan), start=1)
            ]
            self._edges = []
        await self._board()
        await self._commit(self._main_ctx, "PM 規劃：建立任務清單與驗收標準")

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

        # 3) 逐任務迭代：依設定走「波次並行」或循序，兩者共用同一條波次主迴圈。
        all_ok = await self._run_waves(pm_plan + context)

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

        # 6) 視設定自動發佈成果到 GitHub（此時專家團隊仍在線，可在 CI 失敗時修正）
        await self._maybe_publish(done, engineer)

        return {"completed": done, "followups": self._followups, "commit": self._last_commit}

    # --- 波次排程（並行支線）------------------------------------------
    def _llm_semaphore(self) -> asyncio.Semaphore:
        """全域 LLM 並發節流號誌。下限夾到 4，避免單一 lane 內 review 三人 gather 自我死鎖。"""
        if self._llm_sem is None:
            self._llm_sem = asyncio.Semaphore(max(config.LLM_MAX_CONCURRENCY, 4))
        return self._llm_sem

    def _tagged_broadcast(self, task_id: int | None) -> Broadcast:
        """包裝 broadcast：並行 lane 的事件補上 task_id 供前端分流；task_id=None 時原樣直送。"""
        if task_id is None:
            return self.broadcast

        async def _bc(ev: events.StudioEvent) -> None:
            ev.payload.setdefault("task_id", task_id)
            await self.broadcast(ev)

        return _bc

    async def _speak(
        self, ctx: LaneContext, role_key: str, prompt: str, task_id: int | None
    ) -> str:
        """經號誌節流 + 標籤化 broadcast 呼叫某 lane 的專家發言。"""
        async with self._llm_semaphore():
            return await ctx.experts[role_key].speak(prompt, self._tagged_broadcast(task_id))

    def _lane_tag(self, ctx: LaneContext, task: dict) -> int | None:
        """並行 lane（branch 不為 None）回 task id 供事件標籤；主 lane 回 None（行為不變）。"""
        return task["id"] if ctx.branch is not None else None

    async def _run_waves(self, plan_ctx: str) -> bool:
        """把任務分波執行：波次之間循序（尊重依賴），波次之內最多 PARALLEL_LANES 條支線並行。

        關閉並行或無 cwd 時退化成「每任務一波、單一主 lane」，與重構前逐任務循序逐字等價。
        """
        parallel = config.PARALLEL_TASKS_ENABLED and bool(self.cwd)
        # 並行 lane 各自 worktree 需要可分支的 base commit；PM 規劃常無實質檔案（commit 為空），
        # 故先確保主分支有初始 commit。失敗（git 壞）時 worktree 會開不起來 → 走序列化 fallback。
        if parallel and self._last_commit is None:
            self._last_commit = await runner.git_ensure_initial_commit(self.cwd)
        waves = (
            build_waves(self._tasks, self._edges)
            if config.PARALLEL_TASKS_ENABLED
            else [[t] for t in self._tasks]
        )
        all_ok = True
        for wave in waves:
            if self._stop:
                break
            lanes = self._plan_lanes(wave)
            # 序列化開 worktree（git worktree add 不宜並發）；無法隔離者留待序列化重跑。
            opened: list[tuple[LaneContext, list[dict]]] = []
            deferred: list[dict] = []
            for lane_tasks in lanes:
                ctx = await self._open_lane(lane_tasks) if parallel else self._main_ctx
                if ctx is None:
                    deferred.extend(lane_tasks)
                else:
                    opened.append((ctx, lane_tasks))
            results = await asyncio.gather(
                *(self._run_lane(ctx, tasks, plan_ctx) for ctx, tasks in opened),
                return_exceptions=True,
            )
            all_ok = await self._integrate_wave(results, deferred, plan_ctx) and all_ok
        return all_ok

    def _plan_lanes(self, wave: list[dict]) -> list[list[dict]]:
        """把一波任務切成至多 PARALLEL_LANES 條支線（round-robin）。關閉並行/無 cwd 時整波一條。"""
        if not (config.PARALLEL_TASKS_ENABLED and self.cwd):
            return [wave]
        n = max(1, min(config.PARALLEL_LANES, len(wave)))
        lanes: list[list[dict]] = [[] for _ in range(n)]
        for i, task in enumerate(wave):
            lanes[i % n].append(task)
        return [ln for ln in lanes if ln]

    def _lane_worktree_path(self, branch: str) -> Path:
        assert self.cwd is not None
        safe = "".join(c for c in branch if c.isalnum() or c in "-_") or "lane"
        return self.cwd.parent / f"{self.cwd.name}.lanes" / safe

    async def _open_lane(self, lane_tasks: list[dict]) -> LaneContext | None:
        """為一條支線開 git worktree 分支 + 獨立專家團隊。失敗回 None（交由序列化重跑）。"""
        branch = "task-" + "-".join(str(t["id"]) for t in lane_tasks)
        wt = self._lane_worktree_path(branch)
        base = self._last_commit or "HEAD"
        if not await runner.git_worktree_add(self.cwd, wt, branch, base=base):
            await self.broadcast(
                events.phase_change(
                    self.session_id, "並行降級", f"{branch} 無法建立 worktree，改序列化重跑"
                )
            )
            return None
        ctx = LaneContext(branch, wt, {}, branch=branch)
        ctx.experts = self._build_lane_experts(branch, wt)
        self._lane_ctxs.append(ctx)
        return ctx

    def _build_lane_experts(self, suffix: str, cwd: Path) -> dict[str, ExpertLike]:
        """為一條 lane 建一套獨立專家（鏡射主 experts 的角色集合），避免共用對話累積互相污染。"""
        experts = self._get_experts()
        if self._lane_expert_factory is not None:
            factory = self._lane_expert_factory
        else:
            from .providers import make_expert

            factory = make_expert
        return {
            key: factory(experts[key].role, f"{self.session_id}:{suffix}", cwd) for key in experts
        }

    async def _teardown_lane(self, ctx: LaneContext) -> None:
        """收掉一條並行 lane 的專家連線與 worktree（best-effort）。"""
        for ex in list(ctx.experts.values()) + list((ctx.critics or {}).values()):
            try:
                await ex.stop()
            except Exception:  # noqa: BLE001
                pass
        if self.cwd and ctx.cwd and ctx.branch:
            await runner.git_worktree_remove(self.cwd, ctx.cwd, ctx.branch)

    async def _run_lane(
        self, ctx: LaneContext, lane_tasks: list[dict], plan_ctx: str
    ) -> LaneResult:
        """在指定 lane 依序跑完配給的任務（lane 之間由 _run_waves 以 gather 並行）。"""
        lane_ok = True
        for task in lane_tasks:
            if self._stop:
                lane_ok = False
                break
            lane_ok = await self._run_task_in_lane(ctx, task, plan_ctx) and lane_ok
        return LaneResult(ctx=ctx, tasks=lane_tasks, ok=lane_ok)

    async def _run_task_in_lane(self, ctx: LaneContext, task: dict, plan_ctx: str) -> bool:
        """在指定 lane 跑單一任務（實作→驗證→審查→huddle），更新看板與 lane 知識緩衝。"""
        await self.broadcast(
            events.phase_change(self.session_id, "實作", f"任務 #{task['id']}：{task['title']}")
        )
        await self._set_task_status(task, "doing")
        task_ok = await self._work_task(ctx, task, plan_ctx)
        # 卡關升級：跑滿輪數仍未通過 → 召集 huddle 討論替代方案 + 給 1 輪重試。
        if not task_ok and config.HUDDLE_ENABLED and not self._stop:
            task_ok = await self._huddle_and_retry(ctx, task, plan_ctx)
        await self._set_task_status(task, "done" if task_ok else "review")
        # 每任務結束摘要寫進 lane 知識緩衝（波末序列化 flush，供後續波次讀回）。
        if task_ok:
            self._note(ctx, f"## 任務 #{task['id']} 完成：{task['title']}")
        elif task.get("limitation"):
            self._note(
                ctx, f"## 任務 #{task['id']} 已知限制：{task['title']}（huddle 與重試後仍未通過）"
            )
        else:
            self._note(
                ctx, f"## 任務 #{task['id']} 未通過：{task['title']}（標記 review，待後續處理）"
            )
        return task_ok

    async def _integrate_wave(self, results: list, deferred: list[dict], plan_ctx: str) -> bool:
        """波次收尾（全序列化、無競態）：合併各 lane 回主分支 → flush 知識 → 清 worktree。"""
        all_ok = True
        lane_results: list[LaneResult] = []
        for r in results:
            if isinstance(r, BaseException):
                all_ok = False
                await self.broadcast(
                    events.error(self.session_id, f"lane 例外：{type(r).__name__}: {r}")
                )
            else:
                lane_results.append(r)
        # 依 lane_id 穩定排序，逐一合併（主 repo 單一 working tree → 合併必須序列化）。
        for lr in sorted(lane_results, key=lambda x: x.ctx.lane_id):
            if lr.ctx is self._main_ctx:
                all_ok = all_ok and lr.ok
            else:
                all_ok = await self._merge_lane(lr, plan_ctx) and all_ok
                self._flush_lane_notes(lr.ctx)
                await self._teardown_lane(lr.ctx)
        # 無法隔離（worktree 開失敗）的任務：序列化重跑在主 lane。
        for task in deferred:
            if self._stop:
                break
            all_ok = await self._run_task_in_lane(self._main_ctx, task, plan_ctx) and all_ok
        # 主 lane 緩衝（含序列化重跑/降級/循序模式）一併 flush。
        self._flush_lane_notes(self._main_ctx)
        return all_ok

    async def _merge_lane(self, lr: LaneResult, plan_ctx: str) -> bool:
        """把一條並行 lane 的分支合併回主分支；衝突則 abort 後在最新主 HEAD 序列化重跑。"""
        res = await runner.git_merge_worktree(self.cwd, lr.ctx.branch)
        if res.ok:
            h = await runner.git_head_short(self.cwd)
            if h:
                self._last_commit = h  # 下一波 worktree 以此為 base，必含本波已合併變更。
                await self.broadcast(
                    events.git_commit(self.session_id, f"合併支線 {lr.ctx.branch}", h)
                )
            return lr.ok
        if res.conflict:
            await runner.git_merge_abort(self.cwd)
            lr.ctx.notes_buffer.clear()  # 丟棄 lane 嘗試的筆記，改以序列化重跑為準。
            await self.broadcast(
                events.phase_change(
                    self.session_id,
                    "合併衝突",
                    f"支線 {lr.ctx.branch} 與既有變更衝突，於最新主幹序列化重跑",
                )
            )
            ok = True
            for task in lr.tasks:
                if self._stop:
                    ok = False
                    break
                ok = await self._run_task_in_lane(self._main_ctx, task, plan_ctx) and ok
            return ok
        await self.broadcast(
            events.error(self.session_id, f"支線 {lr.ctx.branch} 合併失敗：{res.output[:200]}")
        )
        return False

    async def _work_task(
        self,
        ctx: LaneContext,
        task: dict,
        pm_plan: str,
        *,
        max_rounds: int | None = None,
        seed_feedback: str = "",
    ) -> bool:
        """單一任務的 實作→自測→驗證→審查→改進 迴圈，回傳是否通過。

        所有工作（cwd / 專家 / commit / NOTES）都綁定在傳入的 lane context 上，循序模式
        傳 main_ctx＝今日行為，並行模式傳各 lane 的隔離 context。
        max_rounds：限制本次迴圈輪數（huddle 後重試只給 1 輪）；None 用 config 預設。
        seed_feedback：預先注入的回饋（huddle 結論），非空時第一輪即走「改進」路徑。
        """
        has_security = "security" in ctx.experts
        tag = self._lane_tag(ctx, task)  # 並行 lane 標 task id 供前端分流；主 lane 為 None。
        feedback = seed_feedback
        rounds = max_rounds if max_rounds is not None else config.TASK_MAX_ROUNDS
        impl_history: list[str] = []  # 各輪工程師發言，供停滯偵測
        prev_commit = ctx.last_commit
        for rnd in range(1, rounds + 1):
            if self._stop:
                return False
            human = await self._human_prefix()

            # --- 實作 ---
            if not feedback:
                impl_prompt = (
                    f"{human}{self._notes_context(ctx)}"
                    f"目前要完成的任務 #{task['id']}：{task['title']}\n\n"
                    f"整體計畫供參考：\n{pm_plan}\n\n"
                    "請在工作目錄裡實作，並在交付前自己跑過一次確認能執行。"
                )
            else:
                impl_prompt = (
                    f"{human}任務 #{task['id']}：{task['title']} 尚未通過，"
                    f"請根據以下意見逐項修正（第 {rnd} 輪）：\n\n{feedback}\n\n"
                    "修正後請自己再跑一次確認。"
                )
            impl_text = await self._speak(ctx, "engineer", impl_prompt, tag)

            # --- 交付前自測（確定性 smoke-run）---
            await self._self_test(ctx, impl_text)
            await self._commit(ctx, f"任務#{task['id']} 第{rnd}輪：{task['title']}")

            # --- 停滯守門：連續多輪只重述且無檔案變動 → 提早收斂，不再燒後續 token ---
            impl_history.append(impl_text)
            committed_change = ctx.last_commit != prev_commit
            prev_commit = ctx.last_commit
            if self._stalled(ctx, impl_history, committed_change):
                await self.broadcast(
                    events.phase_change(
                        self.session_id,
                        "停滯收斂",
                        f"任務 #{task['id']} 連續 {config.STALL_ROUNDS} 輪無實質進展，提早結束本任務",
                    )
                )
                self._note(
                    ctx,
                    f"## 停滯收斂 任務 #{task['id']}：{task['title']}"
                    f"（連續 {config.STALL_ROUNDS} 輪只重述，提早收斂）",
                )
                return False

            # --- 驗證 + 審查 + 資安：三者都評同一份已 commit 的實作、互相獨立 → 並行省時 ---
            await self.broadcast(
                events.phase_change(
                    self.session_id,
                    "驗證與審查",
                    f"任務 #{task['id']} 並行驗證/審查/資安（第 {rnd} 輪）",
                )
            )
            await self._set_task_status(task, "review")
            review_calls = [
                self._speak(
                    ctx,
                    "qa",
                    f"請針對任務 #{task['id']}：{task['title']} 的程式碼撰寫並執行測試，"
                    f"驗證是否符合驗收標準：\n\n{pm_plan}",
                    tag,
                ),
                self._speak(
                    ctx,
                    "senior",
                    f"請審查任務 #{task['id']}：{task['title']} 的程式碼（品質、設計、安全），"
                    "並給出決議（`決議: 核可` 或 `決議: 退回`）。",
                    tag,
                ),
            ]
            if has_security:
                review_calls.append(
                    self._speak(
                        ctx,
                        "security",
                        f"請對任務 #{task['id']}：{task['title']} 的程式碼做資安審查，"
                        "輸出 `決議: 安全核可` 或 `決議: 安全退回`（退回時列具體風險）。",
                        tag,
                    )
                )
            results = await asyncio.gather(*review_calls)
            qa_text, senior_text = results[0], results[1]
            sec_text = results[2] if has_security else ""
            qa_ok = qa_passed(qa_text)
            senior_ok = senior_approved(senior_text)
            security_ok = security_approved(sec_text) if has_security else True
            await self.broadcast(
                events.run_result(self.session_id, qa_ok, "驗證通過" if qa_ok else "驗證未通過")
            )

            if qa_ok and senior_ok and security_ok:
                # 放行前異議關卡：用 pm 視角（避開剛審查表態的 senior）獨立挑錯。
                subject = f"任務 #{task['id']}：{task['title']}"
                critic_ok, critic_text = await self._critic_gate(ctx, "pm", subject, pm_plan)
                if critic_ok:
                    return True
                # 異議成立 → 退回再修，把反對理由帶進下一輪並記入知識庫。
                feedback = f"【異議檢查（critic）退回理由】\n{critic_text}"
                self._note(ctx, f"## 異議退回 任務 #{task['id']}：{task['title']}\n{critic_text}")
                await self.broadcast(
                    events.phase_change(
                        self.session_id,
                        "異議退回",
                        f"任務 #{task['id']} 表面通過但 critic 提出實質反對，退回修正",
                    )
                )
                continue

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

    async def _huddle_and_retry(self, ctx: LaneContext, task: dict, context: str) -> bool:
        """卡關升級：召集團隊 huddle 找替代方案 → 給 1 輪重試。

        重試仍失敗則把 task 標為「已知限制」（註記 + 事件），status 由呼叫端維持 review。
        """
        conclusion = await self._huddle(ctx, task, context)
        task_ok = await self._work_task(
            ctx,
            task,
            context,
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

    async def _huddle(self, ctx: LaneContext, task: dict, context: str) -> str:
        """召集卡關討論：依序讓在場角色針對 blocker 提替代方案。回傳彙整結論。

        召集 PM＋架構師＋工程師＋高級工程師（取自該 lane 的專家團隊），缺席角色
        （如 offline 無架構師）自動略過。
        """
        roster = [
            ("pm", ctx.experts.get("pm")),
            ("architect", ctx.experts.get("architect")),
            ("engineer", ctx.experts.get("engineer")),
            ("senior", ctx.experts.get("senior")),
        ]
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
        tag = self._lane_tag(ctx, task)
        notes: list[str] = []
        for key, ex in present:
            prior = ("\n團隊目前的討論：\n" + "\n".join(notes)) if notes else ""
            view = await self._speak(
                ctx,
                key,
                blocker
                + "請針對這個 blocker 提出可突破的替代做法或拆解方式，簡短具體、可立即執行。"
                + prior,
                tag,
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
        self._note(ctx, f"## 卡關討論 任務 #{task['id']}：{task['title']}\n{conclusion}")
        return conclusion

    async def _self_test(self, ctx: LaneContext, impl_text: str) -> None:
        """工程師交付前的確定性 smoke-run（在 lane 的 cwd 內執行），把完整 log 回報。"""
        if not ctx.cwd:
            return
        cmd = runner.parse_run_command(impl_text) or runner.resolve_demo_command(
            ctx.cwd, self._run_command
        )
        if not cmd:
            return
        # 刻意保留 shell（run_command，非 run_command_exec）：cmd 來自 PM/工程師宣告的
        # 自測指令（parse_run_command / resolve_demo_command 動態解析），可能含 pipe /
        # && / glob / 重導向等 shell 語法，須經 /bin/sh 解析；非固定指令、無法 argv 化。
        result = await runner.run_command(ctx.cwd, cmd)  # nosec B602
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
        # 刻意保留 shell：同 _self_test，cmd 為 demo 指令（resolve_demo_command 動態解析），
        # 可能含 shell 語法，必須經 /bin/sh，無法 argv 化。
        result = await runner.run_command(self.cwd, cmd)  # nosec B602
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

        # 最終驗收放行前的異議關卡：用 senior 視角（避開剛驗收表態的 pm）。
        if done:
            critic_ok, _ = await self._critic_gate(
                self._main_ctx, "senior", "整體最終交付成果", "PM 宣告的驗收標準與整體需求"
            )
            done = critic_ok

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
        await self._commit(self._main_ctx, "完成：交付成果與檢討")

        files = workspace.list_files(self.session_id) if self.cwd else []
        await self.broadcast(
            events.StudioEvent(
                events.EventType.DONE,
                self.session_id,
                {"completed": done, "stopped": self._stop, "files": files},
            )
        )
        return done

    async def _maybe_publish(self, done: bool, engineer: ExpertLike | None = None) -> None:
        """專案完成且設定允許時自動發佈到 GitHub；接著驗 CI、失敗讓團隊修正重推、成功合併。

        首輪「等 CI→合併」沿用 publisher.publish(merge=)（REST，結局寫進 result.outcome）；CI 失敗
        則取日誌請 engineer 修正、重推，再以 verify_and_merge 重驗合併，最多 PUBLISH_CI_MAX_ROUNDS 輪。
        engineer 省略（如單測）時不進自我修復迴圈，CI 失敗即保留 PR 待人工。
        """
        if not self.cwd or self._stop or not done:
            return
        if not (config.PUBLISH_AUTO and publisher.is_configured()):
            return
        await self.broadcast(events.phase_change(self.session_id, "發佈", "推送成果到 GitHub"))
        result = await publisher.publish(
            self.cwd, self.session_id, self._requirement, merge=config.PUBLISH_MERGE
        )
        await self.broadcast(events.publish_result(self.session_id, result.to_dict()))
        # 只有「有開 PR、開啟自動合併、且能追蹤 PR 編號」才進入 CI 驗證／自我修復迴圈。
        if not (result.pushed and config.PUBLISH_MERGE and result.pr_number is not None):
            return

        rounds = config.PUBLISH_CI_MAX_ROUNDS
        outcome, detail = result.outcome, result.detail
        for attempt in range(1, rounds + 1):
            if self._stop:
                return
            if outcome == publisher.MergeOutcome.MERGED:
                await self.broadcast(
                    events.ci_result(
                        self.session_id, {"state": "merged", "merged": True, "detail": detail}
                    )
                )
                return
            if outcome != publisher.MergeOutcome.CI_FAILED:
                # CI 已過卻未合併（BLOCKED/CONFLICT）或等待逾時/錯誤：非團隊能修，保留 PR 交人工。
                ui = "error" if outcome == publisher.MergeOutcome.TIMEOUT else "merge_failed"
                await self.broadcast(
                    events.ci_result(
                        self.session_id, {"state": ui, "merged": False, "detail": detail}
                    )
                )
                return
            # CI 失敗：回報本輪結果。
            await self.broadcast(
                events.ci_result(
                    self.session_id,
                    {"state": "fail", "attempt": attempt, "rounds": rounds, "detail": detail},
                )
            )
            # 用完額度（或無可修正的工程師）就放棄，保留 PR 待人工。
            if attempt >= rounds or engineer is None:
                await self.broadcast(
                    events.ci_result(
                        self.session_id,
                        {
                            "state": "giveup",
                            "detail": f"CI 連續 {attempt} 輪未通過，保留 PR 待人工",
                        },
                    )
                )
                return
            # 取失敗日誌→請工程師修正→commit→重推→下一輪 verify_and_merge 重驗新 commit。
            logs = await publisher.ci_failure_logs(result.repo, result.branch, result.branch)
            await self.broadcast(
                events.phase_change(self.session_id, "CI 修正", f"第 {attempt}/{rounds} 輪")
            )
            await engineer.speak(
                await self._human_prefix()
                + "發佈後的 CI/CD 檢查未通過，請依下列失敗日誌修正程式碼，"
                "讓所有測試／檢查都能通過：\n\n" + logs,
                self.broadcast,
            )
            await self._commit(self._main_ctx, f"修正 CI 失敗（第 {attempt} 輪）")
            rp = await publisher.repush(self.cwd, result.branch)
            if not rp.ok:
                await self.broadcast(
                    events.ci_result(
                        self.session_id,
                        {
                            "state": "error",
                            "detail": "re-push 失敗：" + publisher.redact(rp.output),
                        },
                    )
                )
                return
            await self.broadcast(
                events.phase_change(self.session_id, "CI 驗證", f"第 {attempt + 1}/{rounds} 輪")
            )
            outcome, detail = await publisher.verify_and_merge(result.pr_number, result.branch)
