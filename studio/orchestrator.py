"""StudioSession — 工作室的討論/工作流程狀態機（核心）。

Phase 2 流程：PM 拆解結構化任務 → 架構辯論（工程師⇄高級工程師）→ 逐任務迭代
（實作→交付前自測→驗證→審查→帶意見改進，每任務最多 TASK_MAX_ROUNDS 輪）→ 最終實際 Demo
→ PM 驗收 → 團隊檢討。支援人類中途插話與停止。每一步都透過 broadcast callback 送事件。

為了可測試，experts 以 dict 注入；確定性執行（跑程式 / git）集中在 runner，cwd=None 時跳過。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from . import (
    adr,
    appraisal,
    conclusion,
    config,
    events,
    flow,
    lessons,
    memory,
    notify,
    provider_quota,
    publisher,
    reflexion,
    runner,
    workflow as workflow_mod,
    workspace,
)
from .discussion import DiscussionEngine, build_summary

# 純函式層（決議解析／停滯偵測／任務依賴／波次規劃）已移至 flow.py；此處顯式 re-export
# （redundant alias）保住既有 import 路徑（tests、autopilot、improver 皆 from
# studio.orchestrator import ...）。內部呼叫沿用本模組屬性查找，對 studio.orchestrator.<fn>
# 的 monkeypatch 仍然有效。
from .flow import (
    build_waves as build_waves,
    classify_failure_followups as classify_failure_followups,
    critic_blocks as critic_blocks,
    is_stalled as is_stalled,
    parse_clarify as parse_clarify,
    parse_core_changes as parse_core_changes,
    parse_followups as parse_followups,
    parse_followups_meta as parse_followups_meta,
    parse_help_request as parse_help_request,
    parse_incomplete_reason as parse_incomplete_reason,
    parse_lessons as parse_lessons,
    parse_structured_tasks as parse_structured_tasks,
    parse_tasks as parse_tasks,
    parse_tasks_with_deps as parse_tasks_with_deps,
    parse_vision as parse_vision,
    pm_done as pm_done,
    qa_passed as qa_passed,
    security_approved as security_approved,
    senior_approved as senior_approved,
    shippable_verdict as shippable_verdict,
    text_similarity as text_similarity,
)
from .roles import BY_KEY, ROSTER, Role

Broadcast = Callable[[events.StudioEvent], Awaitable[None]]

# 拆解 prompt 的議程格式與粒度守則（micro-rules，字面可 grep 驗證）。{keys}＝本場實際
# 出席角色的 role_key 清單；prompt 的「2–5 個」只是建議不是防線——解析端有
# flow.MAX_AGENDA_ITEMS 硬截斷、分派端有 flow.validate_assignees 硬驗證兜底。
# 並行引導（第五輪 P2）：原版寫「照上述 `任務:`/`依賴:` 行格式」但上文從未定義過該格式，
# PM 靠慣性輸出「實作→接線→補測試→複核」線性依賴鏈——實測 19 場 lanes_max 幾乎全為 1、
# speedup≈1.0，三條 lane 的基礎設施空轉。此處把格式講清楚＋明確要求獨立子任務。
AGENDA_PROMPT_RULES = (
    "另外請輸出討論議程（與任務清單並列，兩者都要）：\n"
    "  - 子題 2–5 個，每個子題獨立一行，格式固定為 "
    "`子題: <標題> | <一句描述> | <成功準則>`；探索型議題允許單子題、不硬拆。\n"
    "  - 每個 `子題:` 行的下一行宣告主責角色 `負責: <role_key>`"
    "（role_key 限定下列其一：{keys}）。\n"
    "  - 任務行格式 `任務: #<id> <標題>`，每任務一句可驗收；依賴行格式 "
    "`依賴: #<後> -> #<前>`（後者須等前者完成才動工）。\n"
    "  - 依賴僅在真有產出先後時才標（B 要用到 A 產出的檔案/介面才算）；測試與文件寫進"
    "對應實作任務的驗收標準，不要另立「補測試」「複核」尾任務串成流水線。\n"
    "  - 能並行就並行：優先拆成 ≥2 個互不依賴、檔案範圍不重疊、可同時動工的任務——"
    "彼此無依賴的任務會被排進同一波次並行執行。\n"
    "  - 若某任務需保護特定檔案不被修改，在該任務行之後輸出 "
    "`禁改: #<id> <pattern>[, <pattern>...]`（逗號分隔多個 pattern）。"
    "pattern 語意：`/` 結尾＝目錄前綴比對、其餘為 PurePath.match 比對（`*` 不跨 `/`）。"
    "範例：`禁改: #2 studio/config.py, docs/`\n"
)

# task_pipeline review stage 的 reviewer：已知核心角色的「專屬 prompt 全文」與「feedback 區段
# 標籤」（保住預設逐字等價）；新角色用 verdict 對應的 generic 指示。{id}{title}{plan} 由 format 帶入。
_REVIEW_PROMPTS = {
    "qa": ("請針對任務 #{id}：{title} 的程式碼撰寫並執行測試，驗證是否符合驗收標準：\n\n{plan}"),
    "senior": (
        "請審查任務 #{id}：{title} 的程式碼（品質、設計、安全），"
        "並給出決議（`決議: 核可` 或 `決議: 退回`）。"
    ),
    "security": (
        "請對任務 #{id}：{title} 的程式碼做資安審查，"
        "輸出 `決議: 安全核可` 或 `決議: 安全退回`（退回時列具體風險）。"
    ),
}
_REVIEW_LABELS = {
    "qa": "驗證工程師回報",
    "senior": "高級工程師審查意見",
    "security": "資安審查意見",
}
# 各 verdict 的輸出格式指示（generic reviewer 用——客製 workflow 指派非核心角色當 reviewer 時，
# 讓它輸出 verdict parser 認得的決議行）。
_VERDICT_INSTRUCTION = {
    "qa_passed": "撰寫並執行測試驗證是否符合驗收標準，最後輸出 `驗證: PASS` 或 `驗證: FAIL`。",
    "senior_approved": "審查品質/設計/安全，輸出 `決議: 核可` 或 `決議: 退回`。",
    "security_approved": "做資安審查，輸出 `決議: 安全核可` 或 `決議: 安全退回`（退回時列具體風險）。",
    "critic_blocks": "挑出『為何這還不算完成』，輸出 `異議: 成立` 或 `異議: 不成立`。",
    "pm_done": "判定是否達成驗收，輸出 `決議: 完成` 或 `決議: 未完成`。",
}


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


log = logging.getLogger("ti.orchestrator")

# lane 收尾（teardown）防爆閥上界（秒）。實例 #261 曾因某 expert `stop()` 在 anyio 吞取消下
# 永不返回，使 `_teardown_lane` 靜默卡死 76 分鐘。此常數為「非營運旋鈕」的硬上界，不進 config.py
# （防爆閥收緊/放寬無運維價值，進 config 反引入頂端＋reload() 兩處同步漂移風險）。
#
# 關鍵：expert stop() 的收斂**不能**靠 `asyncio.timeout`+`gather`——兩者用協作式取消，對「吞取消/
# 阻塞永不返回」的 hang（#261 真正根因，決定性 probe 實證連外層 wait_for 都被拖死）完全穿不透。
# 唯一有界解是 `asyncio.wait(timeout=…)` 取 (done, pending) 後**放手不 await pending**：卡住的 stop()
# 協程洩漏於背景（其子行程已由 stop() 內層 kill 兜底），但主流程於上界內續行。值 120s 遠大於單一
# stop() 的內層 _CTRL_TIMEOUT(=30s)，純作兜底；測試以 monkeypatch 縮小此模組屬性即可秒級驗證。
_TEARDOWN_LANE_TIMEOUT = 120.0


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
        workspace_id: str | None = None,
        clarify: bool | None = None,
        publish_repo: str | None = None,
        base_repo: str | None = None,
        group: dict | None = None,
        time_budget_s: float | None = None,
        auto_publish: bool = True,
        workflow: dict | None = None,
    ):
        self.session_id = session_id
        # 單一事件收斂點：所有事件（含專家送出的 token_usage）都經此包裝，累計 token/成本供每場
        # 用量預算 _budget_exceeded 判斷，再原樣轉送。專家拿到的 broadcast 也是這層（_tagged_broadcast
        # 亦包它），故計數涵蓋全部 lane／reviewer。
        self._broadcast_sink = broadcast
        self.broadcast = self._counting_broadcast
        self.cwd = cwd
        # 檔案面板/下載 API 用的 workspace id；預設＝session_id（一次性 workspace）。
        # 專案模式傳 `project-<pid>`（多場 session 共用同一個固定 workspace）。
        self.workspace_id = workspace_id or session_id
        # 需求澄清：None=依 config.CLARIFY_ENABLED（執行期讀取，reload 即生效）；
        # 自主流程（autopilot／持續改良迴圈）顯式傳 False 跳過——沒有人在等著回答。
        self._clarify = clarify
        self._experts = experts
        # 異議檢查用的獨立 expert 實例（不與主 experts 共用對話/calls 序號）。
        self._critics = critics
        self._intervention = intervention_queue
        self._repo_url = repo_url  # 已 clone 進 workspace 的既有 GitHub repo（可選）
        # 長期專案自己的發佈 repo（owner/repo，可選）：成果改推到該 repo 並對其 base 開 PR，
        # 解決「專案 workspace 與全域發佈 repo 無共同歷史、開不了 PR」的限制。
        self._publish_repo = (publish_repo or "").strip()
        # 是否由本 session 自行發佈：autopilot 顯式傳 False，改由其 wrapper 作為唯一發佈者
        # （等 CI→合併），避免同一份成果被 session 與 autopilot 各開一個 PR（重複 PR）。
        self._auto_publish = auto_publish
        # 目標 repo＝工作基底（owner/repo，可選）：呼叫端僅在 workspace 確實同步自
        # 該 repo（repo_base.ensure_base 的 based）時帶值——prompt 據此告知專家
        # 「既有程式碼就在工作目錄裡」，絕不對專家宣告不存在的基底。
        self._base_repo = (base_repo or "").strip()
        self._tasks: list[dict] = []  # {id, title, status}
        self._edges: list[tuple[int, int]] = []  # 任務依賴邊 (after, before)，並行分波用
        # 議程子題 {title, description, criteria, assignee}（assignee 經 validate_assignees
        # 硬驗證）與修正紀錄 {index, given, assigned}——供逐子題討論與後續持久化（任務 #4）。
        self._agenda: list[dict] = []
        self._agenda_corrections: list[dict] = []
        # 選用的討論小組 {name, role_keys[], mode}（role_store 已驗證）；None＝用預設討論班底。
        # 有值時架構討論階段改以小組成員＋小組 mode 進行（見 _group_participants／_discuss_agenda）。
        self._group = group or None
        # 動態流程定義：None＝載入內建 default_workflow()（等價現有寫死骨架）。直譯器
        # （_run_workflow）按 stages 順序派發 _stage_* handler；客製定義改動順序／參與者／
        # 插 dynamic step。coerce 對壞定義退回預設＋log，執行期不因壞 workflow 崩潰。
        self._workflow = workflow_mod.coerce(workflow)
        self._pending_human = ""  # 並行模式於波次邊界 drain 的插話，套用到該波各 lane
        self._parallel_metrics: dict = {}  # 並行可觀測性：波次/峰值支線/合併衝突/加速比
        # 全域 LLM 並發節流（lazy 建立，綁當前 event loop）；多 lane × 多 reviewer 時生效。
        self._llm_sem: asyncio.Semaphore | None = None
        # 並行 lane 的專家工廠（測試可注入 stub）；None 時用 providers.make_expert。
        self._lane_expert_factory = None
        self._run_command: str | None = None  # PM/工程師宣告的執行指令
        # PM/工程師宣告的 `Demo 網址:`（僅限 localhost）。有宣告＝web 服務型產品，
        # 自測與最終 Demo 改走「啟動服務→HTTP 探測→收掉」，不再傻等常駐指令逾時。
        self._demo_url: str | None = None
        self._requirement = ""
        self._stop = False
        # 軟性時間預算（秒，None=不限）：撞硬 timeout 前主動收斂用。time_budget_s 通常＝autopilot 的
        # 硬 timeout，session 在其 SESSION_SOFT_DEADLINE_FRAC 比例處停止派發新任務、優雅收尾。
        self._time_budget_s = time_budget_s
        self._t0_run: float | None = None  # run() 開工時間戳（_time_exceeded 計時基準）
        self._deadline_hit = False  # 已觸軟性時間預算（停止派發新任務，但仍正常收尾出貨）
        # 每場用量預算（token／USD，0=不限）：與時間預算共用同一條優雅收尾路徑，撞上限前主動收斂，
        # 治「失控場一路燒到撞硬 timeout」。_tokens_used／_usd_used 由 _counting_broadcast 即時累計。
        self._tokens_used = 0
        self._usd_used = 0.0
        self._budget_hit = False  # 觸發的是用量預算（而非時間）→ 收尾事件據此區分措辭
        self._followups: list[str] = []  # 檢討時發現的後續任務（autopilot 回寫 backlog）
        self._followup_items: list[dict] = []  # 同上、含 priority/type（消費端優先用這份）
        self._core_changes: list[
            dict
        ] = []  # 判定需改 Ti 核心的項目（路由到核心 backlog，autopilot 實作開獨立 PR）
        self._vision = ""  # 澄清階段抽出的一句產品願景（回填專案 meta 用）
        self._last_commit: str | None = None  # 最近一次主分支 workspace commit 短 hash
        # 主（循序）lane 的隔離狀態；於 _run 建立後，所有對主 workspace 的操作都走它。
        self._main_ctx: LaneContext | None = None
        # 所有建立過的 lane（含 main），供 run() 結束時統一回收專家、避免子程序洩漏。
        self._lane_ctxs: list[LaneContext] = []
        # 動態流程直譯器的「黑板」：stage handler 間共享的中間產物（取代重構前 _run 的 local
        # 變數）。預設 workflow 走的 handler 與重構前同一段碼、同一順序，故與舊行為等價。
        self._clarify_note = ""
        self._research_notes = ""
        self._pm_plan = ""
        self._design_note = ""
        self._all_ok = False
        self._demo = None
        self._done = False
        self._shippable = False
        # PM 驗收判「未完成」時的 `原因:` 裁決根因（(a)-lite）：隨 run() 回傳給 autopilot，
        # 讓「討論未達完成」的 note 帶結構化原因供分診/回看；完成時恆為空字串。
        self._incomplete_reason = ""
        # 動態招募狀態：本場已招募人數（受 config.RECRUIT_MAX 上限）＋當前 provider 額度快照
        # （動態 stage 開頭查一次，供 PM 額度感知分派與招募自動重綁共用）。_recruit_factory 供測試
        # 注入 stub（簽名 (role, cwd, provider)→expert）；None 時走 providers.make_expert。
        self._recruited = 0
        self._quota_snap: dict | None = None
        self._recruit_factory = None
        # 招募成員「實際綁定」的 provider（key→provider）。招募時 _pick_provider 可能把受限/PM 指定的
        # provider 自動重綁，與 effective_provider(role) 不同；額度摘要/roster 顯示須以此為準才正確。
        self._recruit_providers: dict[str, str] = {}
        # 額度感知 per-task 派工狀態：PM 拆解時的 `派工:` 建議（task_id→{provider, model}）、
        # 已派 provider 序列（同分時避開剛用過的、把任務分攤到各家）、任務期間的暫時綁定
        # （併入 _role_provider_map 供額度摘要顯示正確）。_dispatch_factory 供測試注入 stub
        # （簽名同 providers.make_expert：(role, session_id, cwd, *, provider, model)→expert）。
        self._dispatch_hints: dict[int, dict] = {}
        self._dispatch_recent: list[str] = []
        self._dispatch_bindings: dict[str, str] = {}
        self._dispatch_factory = None
        # auto 派工模式（UI 哨兵檔）：拆解時重讀一次、整場沿用同一值——PM 看到的派工規則
        # 與系統兜底規則同場一致，UI 中途切換從下一場 session 生效。
        self._dispatch_auto = config.dispatch_auto()
        # 3-AI 表決狀態：本場已舉行的表決次數（受 config.VOTE_MAX 上限）。_vote_factory 供測試
        # 注入一次性投票員 stub（簽名同 _recruit_factory：(role, cwd, provider)→expert）；
        # None 時走 providers.make_expert。投票員不進 roster，用完即 stop。
        self._votes_held = 0
        self._vote_factory = None
        # 呼叫端顯式注入 experts（測試 stub／離線假專家）時，per-task 派工不換綁——絕不把
        # stub 換成真 provider 專家（與 _get_critic 的離線護欄同一道理）；注入 _dispatch_factory
        # 的測試除外。
        self._experts_injected = experts is not None
        # 考核（Appraisal）暫存：本場 per-task 客觀指標（task_id → {qa_rounds, qa_passed,
        # senior_approved, provider, model, duration_s, role, token/cost fields}）。qa_* 由 _work_task_rounds
        # 逐輪寫入、provider/model 由 per-task 派工換綁與 _collect_task_perf 補齊，
        # token/cost 由 _counting_broadcast 依 token_usage.task_id 聚合，
        # _wrap_up 時與 PM 的 `考核:` 主觀評分合併寫入 studio/appraisal 考核庫。
        self._task_perf: dict[int, dict] = {}

    # --- 控制 ----------------------------------------------------------
    def request_stop(self) -> None:
        self._stop = True

    def _time_exceeded(self) -> bool:
        """是否已過軟性時間預算（硬 timeout × SESSION_SOFT_DEADLINE_FRAC）。

        刻意與 self._stop 分離：_stop 代表「中止、不出貨」（shippable_verdict 的 stopped 護欄），
        本旗標只代表「時間到、停止派發新任務但仍走 Demo/出貨」——讓已完成的任務能優雅出貨，
        未動的記 known-limit/followup，而非被 autopilot 的 wait_for 硬砍、整場全丟成 timeout。
        無預算或尚未開工一律回 False。觸發後置 self._deadline_hit 供收尾階段發事件。
        """
        if self._time_budget_s is None or self._t0_run is None:
            return False
        elapsed = time.monotonic() - self._t0_run
        if elapsed >= self._time_budget_s * config.SESSION_SOFT_DEADLINE_FRAC:
            self._deadline_hit = True
            return True
        return False

    async def _counting_broadcast(self, ev: events.StudioEvent) -> None:
        """事件單一收斂點：把 token_usage 的 token／成本累進每場用量，再原樣轉送下游 sink。

        其餘事件型別只透傳、零行為改變。容錯：payload 欄位異常一律忽略，絕不讓計數阻斷事件流。
        """
        if getattr(ev, "type", None) == events.EventType.TOKEN_USAGE:
            p = getattr(ev, "payload", None) or {}
            try:
                input_tokens = int(p.get("prompt_tokens") or 0)
                output_tokens = int(p.get("completion_tokens") or 0)
                total_tokens = int(p.get("total_tokens") or 0) or input_tokens + output_tokens
                self._tokens_used += total_tokens
                cost = p.get("cost_usd")
                cost_value = None
                if cost is not None:
                    try:
                        cost_value = float(cost)
                    except (TypeError, ValueError):
                        cost_value = None
                if cost_value is not None:
                    self._usd_used += cost_value
                task_id = p.get("task_id")
                if task_id is not None:
                    perf = self._task_perf.setdefault(int(task_id), {})
                    perf["input_tokens"] = (perf.get("input_tokens") or 0) + input_tokens
                    perf["output_tokens"] = (perf.get("output_tokens") or 0) + output_tokens
                    perf["total_tokens"] = (perf.get("total_tokens") or 0) + total_tokens
                    perf.setdefault("cost_usd", None)
                    perf.setdefault("cost_source", None)
                    if cost_value is not None:
                        perf["cost_usd"] = (perf.get("cost_usd") or 0.0) + cost_value
                        src = perf.get("cost_source")
                        perf["cost_source"] = "reported" if src in (None, "reported") else "mixed"
            except (TypeError, ValueError):
                pass
        await self._broadcast_sink(ev)

    def _budget_exceeded(self) -> bool:
        """是否已過每場用量預算（token 或 USD 任一上限）。

        與 _time_exceeded 同義語：代表「停止派發新任務但仍優雅出貨」，故同樣置 _deadline_hit；
        另置 _budget_hit 讓收尾事件能區分是「用量」而非「時間」觸發。0／未設一律回 False。
        """
        tb = config.SESSION_TOKEN_BUDGET
        ub = config.SESSION_USD_BUDGET
        if (tb > 0 and self._tokens_used >= tb) or (ub > 0 and self._usd_used >= ub):
            self._deadline_hit = True
            self._budget_hit = True
            return True
        return False

    def _should_wind_down(self) -> bool:
        """軟性收尾總閘：時間預算或用量（token／USD）預算任一觸發即收斂。

        各核心迴圈守衛點（派發邊界／_work_task 輪頂／三審前／huddle 前）統一呼叫本閘，
        讓兩類預算共用同一條「停止派發新任務、以已完成成果優雅出貨」路徑。
        """
        return self._time_exceeded() or self._budget_exceeded()

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
        """取出插話、組成要前綴給專家的字串。回顯（human_message）已於收到插話時即時 broadcast
        （見 ws._pump_interventions），此處不重複 broadcast，只負責把文字餵給專家。"""
        human = self._drain_human()
        if not human:
            return ""
        return f"【使用者插話，請納入考量】{human}\n\n"

    async def _lane_human_prefix(self, ctx: LaneContext) -> str:
        """lane 內取插話前綴。並行 lane：先把佇列中新到的插話 drain 進來、累加到 _pending_human，
        讓波次中途送的插話也即時生效（不必等下一波）。_drain_human 走 get_nowait 同步取出、過程
        無 await＝在 event loop 內為原子操作，多 lane 並行呼叫不會取到同一則；回顯已於收到時即時
        broadcast，故此處不再廣播。主 lane / 循序維持每次發言即時 drain 的既有行為。"""
        if ctx.branch is not None:
            fresh = self._drain_human()
            if fresh:
                self._pending_human = (
                    f"{self._pending_human}\n{fresh}".strip() if self._pending_human else fresh
                )
            return (
                f"【使用者插話，請納入考量】{self._pending_human}\n\n"
                if self._pending_human
                else ""
            )
        return await self._human_prefix()

    async def _await_human(self, timeout_s: float) -> str:
        """阻塞等待一則人類插話（給需求澄清用），逾時或被要求停止回空字串。

        以 1 秒切片輪詢，確保等待期間 stop 指令仍即時生效——流程絕不因等人而卡死。
        """
        if self._intervention is None:
            return ""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        while not self._stop:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return ""
            try:
                first = await asyncio.wait_for(
                    self._intervention.get(), timeout=min(1.0, remaining)
                )
            except asyncio.TimeoutError:
                continue
            rest = self._drain_human()
            return (first + ("\n" + rest if rest else "")).strip()
        return ""

    # --- 需求澄清（拆解前的反問階段）-----------------------------------
    async def _clarify_requirement(self, pm: ExpertLike, requirement: str) -> str:
        """PM 檢視需求；模糊則向使用者反問關鍵問題（附預設假設），逾時按假設續行。

        回傳要前綴給調研／拆解 prompt 的澄清結論（未啟用／不需澄清回空字串）。
        僅互動 session 生效：無插話佇列（autopilot）、離線 demo、或顯式關閉時跳過。
        """
        enabled = config.CLARIFY_ENABLED if self._clarify is None else self._clarify
        if not enabled or self._intervention is None or config.OFFLINE_MODE or self._stop:
            return ""
        await self.broadcast(
            events.phase_change(self.session_id, "需求澄清", "PM 檢視需求是否足夠明確")
        )
        text = await pm.speak(
            f"使用者的產品需求如下：\n\n{requirement}\n\n"
            "請判斷此需求是否足夠明確、可直接拆解動工。若是，輸出一行 `澄清: 不需要`。\n"
            f"若否，向使用者反問最多 {config.CLARIFY_MAX_QUESTIONS} 個最關鍵的問題"
            "（只問會改變做法的，不問瑣碎細節），每個問題固定兩行：\n"
            "`問題: <一句具體的問題>`\n"
            "`假設: <若使用者未回覆，你將採用的合理預設>`\n"
            "無論是否需要澄清，最後都補一行 `願景: <一句產品願景>`（給長期專案定方向用）。",
            self.broadcast,
        )
        # 願景回填：抽出一句產品願景（專案 meta 為空時由 ws 回填，給後續場次定方向）。
        self._vision = parse_vision(text)
        questions = parse_clarify(text)[: config.CLARIFY_MAX_QUESTIONS]
        if not questions:
            return ""
        timeout = config.CLARIFY_TIMEOUT
        await self.broadcast(events.clarify_request(self.session_id, questions, timeout))
        await self.broadcast(
            events.phase_change(
                self.session_id,
                "需求澄清",
                f"等待你的回覆（{int(timeout)} 秒內未回覆將按 PM 的預設假設進行）",
            )
        )
        answer = await self._await_human(timeout)
        qa_lines = [
            f"- 問題：{q['q']}\n  假設：{q.get('assumption') or '（未提供）'}" for q in questions
        ]
        if answer:
            await self.broadcast(
                events.phase_change(self.session_id, "需求澄清", "已收到回覆，納入需求")
            )
            note = (
                "【需求澄清】PM 的提問與預設假設：\n"
                + "\n".join(qa_lines)
                + f"\n\n使用者的回覆（以此為準，覆蓋上列假設）：\n{answer}\n\n"
            )
        else:
            await self.broadcast(
                events.phase_change(self.session_id, "需求澄清", "未收到回覆，按預設假設進行")
            )
            note = (
                "【需求澄清】曾向使用者提問但未獲回覆，依下列預設假設進行：\n"
                + "\n".join(qa_lines)
                + "\n\n"
            )
        self._write_prd(requirement, questions, answer)
        return note

    def _write_prd(self, requirement: str, questions: list[dict], answer: str) -> None:
        """把需求與澄清結論固化成 workspace 內的 PRD.md（追加；專案模式跨場次累積）。

        寫檔失敗只略過，不影響流程；隨後的「PM 規劃」commit 會把它一併入庫。
        """
        if self.cwd is None:
            return
        path = self.cwd / "PRD.md"
        lines = []
        if not path.exists():
            lines.append("# 產品需求紀錄（PRD）\n")
        lines.append(f"## 需求（{time.strftime('%Y-%m-%d %H:%M')}）\n")
        lines.append(requirement + "\n")
        if questions:
            lines.append("### 澄清問答\n")
            for q in questions:
                lines.append(f"- 問題：{q['q']}")
                lines.append(f"  - 預設假設：{q.get('assumption') or '（未提供）'}")
            lines.append(f"- 使用者回覆：{answer or '（未回覆，採上列假設）'}\n")
        try:
            with path.open("a", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
        except OSError:
            pass

    def _get_experts(self) -> dict[str, ExpertLike]:
        if self._experts is None:
            assert self.cwd is not None
            self._experts = _build_experts(self.session_id, self.cwd)
        return self._experts

    async def _idle_reaper(self) -> None:
        """背景回收閒置專家(B1):每 60s 掃描,閒置逾 EXPERT_IDLE_STOP_S 即 release。

        只處理有 idle_for/release 介面的專家(Claude Expert;其他 provider 無常駐子行程,
        duck-typing 跳過);豁免 EXPERT_IDLE_STOP_EXEMPT(預設 pm,脈絡最值錢);in-flight
        由 release() 自身防護。任何例外吞掉——回收是加值不是依賴,絕不弄死 session。
        """
        while True:
            await asyncio.sleep(60)
            try:
                seen: list = []
                for ctx in self._lane_ctxs:
                    seen += list(ctx.experts.values())
                    seen += list((ctx.critics or {}).values())
                seen += list((self._experts or {}).values())
                for ex in dict.fromkeys(seen):
                    role_key = getattr(getattr(ex, "role", None), "key", "").lower()
                    if role_key in config.EXPERT_IDLE_STOP_EXEMPT:
                        continue
                    idle_for = getattr(ex, "idle_for", None)
                    release = getattr(ex, "release", None)
                    if idle_for is None or release is None:
                        continue
                    if idle_for() > config.EXPERT_IDLE_STOP_S:
                        await release()
                        log.info("閒置回收專家 %s(>%ds)", role_key, config.EXPERT_IDLE_STOP_S)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — 回收失敗不得影響 session
                log.debug("idle reaper 掃描失敗(忽略)", exc_info=True)

    # --- 異議檢查（critic）-------------------------------------------------
    def _get_critic(self, ctx: LaneContext, role_key: str) -> ExpertLike | None:
        """取得指定視角的獨立 critic expert（綁定到傳入 lane 的 cwd/critics）。

        優先用該 lane 已注入/建立的 critics（測試/離線）；否則在有 cwd 時以獨立 session 建一個
        新實例，確保不污染該 lane 主 experts 的對話與 calls 序號。都無法取得時回 None（放行）。
        """
        # 已有該視角的 critic 即複用;dict 存在但缺此 role 時要「補建併入」而非回 None——
        # 舊寫法 `ctx.critics = {role_key: critic}` 整個覆蓋,第二種視角永遠拿到 None 靜默
        # 放行(critic gate 形同虛設,2026-07-10 效能檢討時發現)。測試/離線注入的 critics
        # (無 cwd)維持原語意:缺席=放行。
        if ctx.critics is not None and role_key in ctx.critics:
            return ctx.critics[role_key]
        # 離線示範未注入 critics 時不走真 provider（無金鑰），直接放行不報錯。
        if ctx.cwd is None or config.OFFLINE_MODE:
            return None if ctx.critics is None else ctx.critics.get(role_key)
        from .providers import make_expert
        from .roles import BY_KEY

        critic = make_expert(BY_KEY[role_key], f"{self.session_id}:critic:{role_key}", ctx.cwd)
        ctx.critics = {**(ctx.critics or {}), role_key: critic}
        return critic

    async def _critic_gate(
        self,
        ctx: LaneContext,
        role_key: str,
        subject: str,
        acceptance: str,
        broadcast: Broadcast | None = None,
    ) -> tuple[bool, str]:
        """放行前的異議關卡。回傳 (是否放行, critic 文字)。

        刻意只餵標的與驗收標準、不餵當事人剛才的核可理由以降低錨定；停用或無 critic 時放行。
        任務路徑傳入 tagged broadcast；主 lane 只標 token_usage，並行 lane 才標全部任務事件。
        """
        # 離線示範（OFFLINE_MODE）視為 demo 情境自動啟用，以展示「內部討論」事件。
        if not (config.CRITIC_ENABLED or config.OFFLINE_MODE) or self._stop:
            return True, ""
        critic = self._get_critic(ctx, role_key)
        if critic is None:
            return True, ""
        bc = broadcast or self.broadcast
        async with self._llm_semaphore():
            text = await critic.speak(
                "你是獨立的異議檢查者，專挑『為何這還不算完成』，以防團隊形成錯誤共識。\n"
                f"檢查標的：{subject}\n\n驗收標準：\n{acceptance}\n\n"
                "請只根據標的與驗收標準判斷，提出具體、實質的反對；找不到實質問題就放行。\n"
                "最後一行明確輸出：`異議: 成立`（需退回）或 `異議: 不成立`（放行）。",
                bc,
            )
        blocks = critic_blocks(text)
        await bc(events.critic_review(self.session_id, role_key, not blocks, text))
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
            # 以 workspace_id 定位（專案模式下多場 session 共用同一份 NOTES.md，知識跨場次累積）。
            workspace.append_note(self.workspace_id, note)
        ctx.notes_buffer.clear()

    def _notes_context(self, ctx: LaneContext) -> str:
        """讀回 NOTES.md，組成要注入實作 prompt 的前綴（停用/空白時回空字串）。

        只取尾段 NOTES_MAX_CHARS 字（從段落邊界起切）：專案模式 NOTES.md 跨場次累積、
        只增不減，全文注入會讓 context 無限膨脹。
        """
        if not (config.NOTES_ENABLED and ctx.cwd):
            return ""
        notes = workspace.read_notes(self.workspace_id).strip()
        if not notes:
            return ""
        cap = config.NOTES_MAX_CHARS
        if cap > 0 and len(notes) > cap:
            tail = notes[-cap:]
            cut = tail.find("\n\n")
            if 0 <= cut < len(tail) - 2:
                tail = tail[cut + 2 :]
            notes = tail.strip()
        return f"【團隊共用知識庫 NOTES.md（過往踩過的坑／決策／後續）】\n{notes}\n\n"

    # --- 知識沉澱（docs/RESEARCH.md；PRD 由澄清階段、設計決策由 ADR 寫根目錄）---
    def _knowledge_tail(self, name: str) -> str:
        """讀回 workspace docs/<name> 的尾段供注入 prompt（停用／無 cwd／不存在回空字串）。"""
        if not (config.KNOWLEDGE_ENABLED and self.cwd):
            return ""
        return workspace.read_doc_tail(self.workspace_id, name, config.KNOWLEDGE_MAX_CHARS)

    def _persist_knowledge(self, name: str, text: str) -> None:
        """把一段知識追加到 workspace docs/<name>（停用／無 cwd／空字串時略過）。

        以 workspace_id 定位：專案模式下多場 session 共用同一 workspace，知識跨場次累積。
        """
        if config.KNOWLEDGE_ENABLED and self.cwd and (text or "").strip():
            workspace.append_doc(self.workspace_id, name, text)

    def _persist_improvement_plan(self, retro: str) -> None:
        """把檢討的「後續改善任務＋可重用教訓」沉澱成 docs/IMPROVEMENT.md（可累積的改善計畫）。

        純加性、不影響完成判定：從 retro 文字解析（與 backlog 回填同一份解析，不重跑 LLM），
        無項目／停用／無 cwd 時自然略過（走 _persist_knowledge 的既有閘）。
        """
        items = parse_followups_meta(retro)
        plan_lessons = parse_lessons(retro) if config.LESSONS_ENABLED else []
        if not items and not plan_lessons:
            return
        lines = [f"## 改善計畫 — {self._requirement[:60]}"]
        if items:
            lines.append("### 後續改善任務")
            lines += [f"- [P{it['priority']}/{it['type']}] {it['title']}" for it in items]
        if plan_lessons:
            lines.append("### 可重用教訓")
            lines += [f"- {ln}" for ln in plan_lessons]
        self._persist_knowledge("IMPROVEMENT.md", "\n".join(lines) + "\n")

    def _merge_followup_items(
        self, retro_items: list[dict], failed_titles: list[str] | None = None
    ) -> None:
        """累加結構化後續任務；客觀失敗同標題時升格為 P0 bug。"""
        merged = classify_failure_followups(failed_titles or [], self._followup_items + retro_items)
        self._followup_items = merged
        seen = set(self._followups)
        for item in merged:
            title = item["title"]
            if title not in seen:
                seen.add(title)
                self._followups.append(title)

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

    async def _set_task_status(
        self, task: dict, status: str, broadcast: Broadcast | None = None
    ) -> None:
        task["status"] = status
        # 任務路徑傳入 tagged broadcast：主 lane 只標 token_usage，並行 lane 才標全部任務事件。
        # 看板是 session 全域快照（跨所有任務）→ 維持未標籤的 self.broadcast。
        bc = broadcast or self.broadcast
        await bc(events.task_status(self.session_id, task["id"], task["title"], status))
        await self._board()

    # --- git --------------------------------------------------------------
    async def _commit(
        self,
        ctx: LaneContext,
        message: str,
        broadcast: Broadcast | None = None,
        *,
        forbidden_paths: list[str] | None = None,
    ) -> list[str]:
        if not ctx.cwd:
            return []
        if forbidden_paths:
            result = await runner.git_commit(ctx.cwd, message, forbidden_paths=forbidden_paths)
        else:
            result = await runner.git_commit(ctx.cwd, message)
        violations: list[str] = []
        if isinstance(result, runner.GitCommitResult):
            h = result.commit_hash
            violations = result.forbidden_violations
        else:
            h = result
        bc = broadcast or self.broadcast
        if violations:
            detail = f"commit 被禁改清單擋下：{', '.join(violations)}"
            await bc(events.phase_change(self.session_id, "禁改路徑違規", detail))
            await bc(
                events.StudioEvent(
                    events.EventType.RUN_RESULT,
                    self.session_id,
                    {
                        "passed": False,
                        "detail": detail,
                        "log": "\n".join(violations),
                        "forbidden_violations": violations,
                    },
                )
            )
            return violations
        if h:
            ctx.last_commit = h
            # 主分支（branch=None）的 commit 同步到 session 級欄位（發佈/回傳值仍用它）。
            # 並行 lane 的 commit 不動 self._last_commit，改由波次合併後以主分支 HEAD 更新。
            if ctx.branch is None:
                self._last_commit = h
            # 任務路徑傳入 tagged broadcast：主 lane 只標 token_usage，並行 lane 才標全部任務事件。
            await bc(events.git_commit(self.session_id, message, h))
        return []

    @staticmethod
    def _forbidden_paths_feedback(task: dict, violations: list[str]) -> str:
        items = "\n".join(f"- {path}" for path in violations)
        return (
            "【禁改路徑違規】本輪修改了任務宣告不可改的路徑，commit 已被擋下。\n"
            "請回復這些檔案的變更，改用允許範圍完成任務，修正後重新自測。\n"
            f"任務 #{task['id']}：{task['title']}\n"
            f"違規檔案：\n{items}"
        )

    # --- 辯論 ----------------------------------------------------------
    async def _debate(self, a: ExpertLike, b: ExpertLike, topic: str, rounds: int) -> None:
        """a 提案、b 點評、a 回應，來回 rounds 輪。rounds<=0 則跳過。

        ADR 開啟時，辯論結束後由 b（高級工程師）把共識蒸餾成決策行並落盤——
        讓純辯論路徑（無架構師）的結論也能跨場次留痕。
        """
        if rounds <= 0 or self._stop:
            return
        # 分流：TI_DISCUSS_MODE=round_robin|parallel 時走 DiscussionEngine；
        # 未設或 legacy（含非法值 fallback）時下方原始路徑一行不動（向後相容）。
        if config.DISCUSS_MODE in ("round_robin", "parallel"):
            await self._debate_via_engine(a, b, topic)
            return
        await self.broadcast(
            events.phase_change(self.session_id, "架構討論", "工程師與高級工程師對齊做法")
        )
        proposal = await a.speak(
            adr.context(self.cwd) + f"{topic}\n請先簡短提出你打算採取的整體做法與檔案結構。",
            self.broadcast,
        )
        critique = ""
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
        if config.ADR_ENABLED and self.cwd and not self._stop:
            distilled = await b.speak(
                "把剛才架構討論的共識蒸餾成決策記錄：每條獨立、逐行輸出 `決策: <結論>`，"
                "重要取捨可緊接補 `理由: <為何>` 與 `否決: <被否決的替代方案>` 行。"
                "只輸出格式行。\n\n"
                f"【提案】{proposal}\n\n【點評】{critique}",
                self.broadcast,
            )
            if adr.record(self.cwd, adr.parse_adr(distilled), session_id=self.session_id):
                await self._commit(self._main_ctx, "架構決策：記錄 ADR")

    async def _debate_via_engine(self, a: ExpertLike, b: ExpertLike, topic: str) -> None:
        """DiscussionEngine 路徑（TI_DISCUSS_MODE=round_robin|parallel）。

        外部資源全部注入（semaphore／broadcast／should_stop），discussion.py 不回頭
        import orchestrator。結束後沿用既有 ADR 蒸餾落盤：蒸餾 prompt 餵
        summary.final_positions 串接＋末輪 transcript（取代舊 proposal/critique 兩變數），
        蒸餾指令與 adr.record 與舊路徑一致。
        """
        await self.broadcast(
            events.phase_change(
                self.session_id, "架構討論", f"多角色討論（{config.DISCUSS_MODE}）對齊做法"
            )
        )
        engine = DiscussionEngine(
            participants=[(a.role.name, a), (b.role.name, b)],
            mode=config.DISCUSS_MODE,
            max_rounds=max(config.DISCUSS_MAX_ROUNDS, 1),
            semaphore=self._llm_semaphore(),
            broadcast=self.broadcast,
            should_stop=lambda: self._stop,
        )
        result = await engine.run(adr.context(self.cwd) + f"{topic}\n請對齊整體做法與檔案結構。")
        if not (config.ADR_ENABLED and self.cwd and not self._stop and result.transcript):
            return
        positions = "\n\n".join(
            f"【{name} 最終立場】{text}" for name, text in result.summary["final_positions"].items()
        )
        last_round = result.transcript[-1].round
        last_texts = "\n\n".join(
            f"@{u.speaker}：{u.text}" for u in result.transcript if u.round == last_round
        )
        distilled = await b.speak(
            "把剛才架構討論的共識蒸餾成決策記錄：每條獨立、逐行輸出 `決策: <結論>`，"
            "重要取捨可緊接補 `理由: <為何>` 與 `否決: <被否決的替代方案>` 行。"
            "只輸出格式行。\n\n"
            f"{positions}\n\n【末輪發言】\n{last_texts}",
            self.broadcast,
        )
        if adr.record(self.cwd, adr.parse_adr(distilled), session_id=self.session_id):
            await self._commit(self._main_ctx, "架構決策：記錄 ADR")

    def _group_participants(
        self, experts: dict[str, ExpertLike]
    ) -> tuple[str, list[tuple[str, ExpertLike]]] | None:
        """選用討論小組時，把小組 role_keys 解析成 ``(mode, [(name, expert), ...])``。

        成員以本場出席的 ``experts`` 為準解析（不在場者略過＋log，不靜默吞）；可解析成員
        <2 時退回 None＝用預設討論班底——避免「小組成員都不在場」默默退化成單人討論。
        以實例去重（防同一 expert 被列兩次）。回傳的 mode 為小組自身的 mode（白名單已由
        role_store.validate_group 在寫入時保證 ∈ {round_robin, parallel}）。
        """
        if not self._group:
            return None
        members: list[tuple[str, ExpertLike]] = []
        for key in self._group.get("role_keys", []):
            ex = experts.get(key)
            if ex is None:
                log.warning(
                    "討論小組 %r 成員 %r 不在本場出席角色集合，略過",
                    self._group.get("name"),
                    key,
                )
                continue
            if all(ex is not p for _, p in members):
                members.append((ex.role.name, ex))
        if len(members) < 2:
            log.warning(
                "討論小組 %r 可解析成員不足 2 名，退回預設討論班底", self._group.get("name")
            )
            return None
        return self._group.get("mode") or config.DISCUSS_MODE, members

    @staticmethod
    def _proposer_first(
        members: list[tuple[str, ExpertLike]], assignee_key: str
    ) -> list[tuple[str, ExpertLike]]:
        """把主責（assignee）排到討論班底首位取得提案先發言權；不在班底則原序不動。"""
        idx = next(
            (
                i
                for i, (_, ex) in enumerate(members)
                if getattr(ex.role, "key", None) == assignee_key
            ),
            None,
        )
        if idx in (None, 0):
            return members
        return [members[idx]] + members[:idx] + members[idx + 1 :]

    async def _discuss_agenda(
        self,
        experts: dict[str, ExpertLike],
        engineer: ExpertLike,
        senior: ExpertLike,
        requirement: str,
    ) -> str:
        """逐子題多角色討論（TI_DISCUSS_MODE=round_robin|parallel 且無架構師時的討論階段）。

        每個子題以 self._agenda 的 assignee（已硬驗證）為提案方——排 participants 首位
        取得先發言權，topic 文字標明「主責: <角色名>」；engineer/senior 為固定討論班底
        （與 assignee 以實例去重）。多子題時每子題輪數走 config.AGENDA_ROUNDS（預設 1，
        成本上界 5×1）；單子題（探索型/解析 fallback）沿用 DISCUSS_MAX_ROUNDS 與既有
        engine 路徑行為對齊。引擎介面不動，只改呼叫端。

        全部子題討論完後收斂為一次：各子題 final_positions 串接成單一結論文字，ADR 開啟
        時做一次蒸餾、一筆 adr.record、一次 commit（絕不逐子題蒸餾——省 token 也避免
        後續子題吃到前面子題決策造成干擾）。回傳串接結論作 design_note 供逐任務脈絡。
        """
        if config.DEBATE_ROUNDS <= 0 or self._stop:
            return ""
        agenda = self._agenda
        multi = len(agenda) > 1
        rounds = config.AGENDA_ROUNDS if multi else max(config.DISCUSS_MAX_ROUNDS, 1)
        # 選用討論小組時：班底＝小組成員、mode＝小組 mode；否則用預設（assignee＋eng＋senior）。
        grp = self._group_participants(experts)
        mode = grp[0] if grp else config.DISCUSS_MODE
        detail = f"逐子題多角色討論（{mode}，{len(agenda)} 個子題）"
        if grp:
            detail += f"｜討論小組「{self._group['name']}」（{len(grp[1])} 人）"
        await self.broadcast(events.phase_change(self.session_id, "架構討論", detail))
        conclusions: list[str] = []
        all_transcript: list = []  # 跨子題聚合，供討論收斂後一次結論彙整落盤
        for idx, item in enumerate(agenda, start=1):
            if self._stop:
                break
            assignee_key = item.get("assignee", "")
            if grp:
                # 小組固定班底；主責（assignee）若在小組內排首位取得提案先發言權，
                # 否則沿用小組原序首位提案。
                participants = self._proposer_first(grp[1], assignee_key)
            else:
                # 預設班底：assignee（已 validate_assignees 硬驗證；空＝極端案例兜底 engineer）
                # ＋ engineer ＋ senior，以實例去重，確保提案方永遠存在。
                assignee = experts.get(assignee_key) or engineer
                participants = []
                for ex in (assignee, engineer, senior):
                    if all(ex is not p for _, p in participants):
                        participants.append((ex.role.name, ex))
            topic_lines = [f"議程子題 {idx}/{len(agenda)}：{item['title']}"]
            if item.get("description"):
                topic_lines.append(f"描述: {item['description']}")
            if item.get("criteria"):
                topic_lines.append(f"成功準則: {item['criteria']}")
            topic_lines.append(f"主責: {participants[0][0]}（先發言提案，其他人接著點評）")
            engine = DiscussionEngine(
                participants=participants,
                mode=mode,
                max_rounds=rounds,
                semaphore=self._llm_semaphore(),
                broadcast=self.broadcast,
                should_stop=lambda: self._stop,
            )
            result = await engine.run(
                adr.context(self.cwd)
                + f"我們要實作這個需求：{requirement}\n"
                + "\n".join(topic_lines)
                + "\n請對齊此子題的做法與檔案結構。"
            )
            if result.transcript:
                all_transcript.extend(result.transcript)
                positions = "\n".join(
                    f"【{name} 最終立場】{text}"
                    for name, text in result.summary["final_positions"].items()
                )
                conclusions.append(f"〔子題 {idx}：{item['title']}〕\n{positions}")
        if not conclusions:
            return ""
        merged = "\n\n".join(conclusions)
        # 結論彙整落盤（與 ADR 解耦）：討論全部收斂後一次彙整→落盤→commit→broadcast。
        # 必須在下方「ADR 關閉即提前 return」之前——CONCLUSION.md 不應因 ADR 關閉而不產出。
        await self._record_conclusion(senior, all_transcript)
        if not (config.ADR_ENABLED and self.cwd and not self._stop):
            return merged
        distilled = await senior.speak(
            "把剛才各子題架構討論的共識蒸餾成決策記錄：每條獨立、逐行輸出 `決策: <結論>`，"
            "重要取捨可緊接補 `理由: <為何>` 與 `否決: <被否決的替代方案>` 行。"
            "只輸出格式行。\n\n" + merged,
            self.broadcast,
        )
        if adr.record(self.cwd, adr.parse_adr(distilled), session_id=self.session_id):
            await self._commit(self._main_ctx, "架構決策：記錄 ADR")
        return merged

    async def _record_conclusion(self, senior: ExpertLike, transcript: list) -> None:
        """討論收斂後彙整→落盤 CONCLUSION.md→commit→broadcast 一筆結論事件（單一接點）。

        一場一次的終局快照：以跨子題聚合的整場 transcript 算規則式 summary，交 senior
        蒸餾出四段結論（漏標前綴則 fallback 回規則骨架，見 conclusion.summarize），落
        workspace 根、進 git，再廣播 CONCLUSION 事件。

        時序保證（架構決策）：commit **先於** broadcast——先確保檔案入 git 再通知，避免
        前端收到事件但檔案尚未落盤的空窗。落盤是事實來源、事件僅通知；broadcast 不回滾
        已完成的 record/commit。無 cwd／已停止／空 transcript 時直接略過（不阻斷主流程）。
        """
        if not (self.cwd and not self._stop and transcript):
            return
        await self.broadcast(
            events.phase_change(self.session_id, "結論彙整", "彙整討論結論並產出 CONCLUSION.md")
        )
        summary = build_summary(transcript)
        result = await conclusion.summarize(senior, summary, transcript, self.broadcast)
        # 帶入真實輪數供 sidecar 機讀（設計決策 #4）：取整場 transcript 的末輪 round。
        path = conclusion.record(
            self.cwd,
            result,
            session_id=self.session_id,
            rounds=max((u.round for u in transcript), default=0),
        )
        if path is None:
            return
        await self._commit(self._main_ctx, "結論彙整：產出 CONCLUSION.md")
        await self.broadcast(events.conclusion(self.session_id, str(path), result))

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
            adr.context(self.cwd)
            + rnote
            + topic
            + "\n\n請提出整體設計：技術選型、模組邊界、資料流與關鍵取捨。",
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
        adr_note = (
            "重要取捨可在決策行後緊接補 `理由: <為何>` 與 `否決: <被否決的替代方案>` 行（會記入決策檔）。"
            if config.ADR_ENABLED
            else ""
        )
        decision = await architect.speak(
            f"綜合以下意見定案，逐行輸出 `設計決策: <決策>`。{adr_note}\n\n"
            f"【工程師】{eng_view}\n\n【高級工程師】{senior_view}",
            self.broadcast,
        )
        if config.ADR_ENABLED and self.cwd:
            if adr.record(self.cwd, adr.parse_adr(decision), session_id=self.session_id):
                await self._commit(self._main_ctx, "架構決策：記錄 ADR")
        return decision

    # --- 主流程 --------------------------------------------------------
    async def run(self, requirement: str) -> dict:
        """執行整場討論。回傳結果摘要供 autopilot 使用（前端走 broadcast，不需回傳值）。"""
        result = {
            "completed": False,
            "incomplete_reason": "",
            "followups": [],
            "followup_items": [],
            "core_changes": [],
            "commit": None,
            "vision": "",
            "provider_unavailable": "",
        }
        # 開工前先寫 baseline .gitignore（純檔案寫入、不需 .git）:讓 SDK 沙箱散落的 dotfiles／
        # .venv／*.db 等 junk 從不被 `git add -A` 追蹤——乾淨歷史＋乾淨 lane 分支（與 #126 發佈前
        # 兜底淨化互補）。cwd=None 的單元測試自然略過。
        if self.cwd is not None:
            runner.write_baseline_gitignore(self.cwd)
        self._t0_run = time.monotonic()  # 軟性時間預算計時基準
        # 專家閒置回收 reaper(B1,預設關):每 60s 掃全 lane 專家,閒置逾 TTL 且非發言中即
        # release(斷線回收 SDK 子行程;下次 speak 自動重連)。豁免角色見 config。
        reaper = asyncio.create_task(self._idle_reaper()) if config.EXPERT_IDLE_STOP_S > 0 else None
        try:
            result = await self._run(requirement)
        except Exception as exc:  # noqa: BLE001 — 任何錯誤都回報給前端而非崩潰
            provider = getattr(exc, "provider", "")
            if provider:
                result["provider_unavailable"] = str(provider)
                self._stop = True
                await self.broadcast(
                    events.phase_change(
                        self.session_id,
                        "Provider 暫停",
                        f"{provider} 暫時不可用，本場停止以避免錯誤被當成 QA 失敗重跑。",
                    )
                )
            await self.broadcast(events.error(self.session_id, f"{type(exc).__name__}: {exc}"))
        finally:
            if reaper is not None:
                reaper.cancel()
                try:
                    await reaper
                except (asyncio.CancelledError, Exception):  # noqa: BLE001 — reaper 收尾絕不影響回收
                    pass
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
            # 兜底清理並行 lane 的 worktree：正常路徑已在 _integrate_wave 逐一 teardown；此處
            # 涵蓋 lane 例外 / 中途停止等未走到 teardown 的情況，避免 .lanes worktree 目錄與
            # git worktree 註冊洩漏（該目錄是 workspace 的兄弟目錄，不會被 history 回收掃到）。
            if self.cwd:
                for ctx in self._lane_ctxs:
                    if ctx.branch and ctx.cwd and ctx.cwd.exists():
                        try:
                            await runner.git_worktree_remove(self.cwd, ctx.cwd, ctx.branch)
                        except Exception:  # noqa: BLE001
                            pass
                # 只清「本場」的 lane 目錄（lane-{session_id}-* 前綴，見 _open_lane 的分支命名），
                # 絕不 rmtree 整個 lanes root——root 是所有 session 共用的兄弟目錄：被放棄的舊
                # session（_cancel_and_reclaim 60s 放棄後本 finally 仍在慢跑，每個 wedged 專家
                # disconnect 有 30s 上限）若整鍋端走，會把「下一個任務」正在用的活 lane 一併刪除，
                # SDK 子行程以已消失的 cwd spawn 直接 FileNotFoundError、整波 CLIConnectionError
                # 重試（2026-07-10 journal 實證：lane-apbbe769ec75-3）。root 只在已空時順手移除。
                lanes_root = self.cwd.parent / f"{self.cwd.name}.lanes"
                if lanes_root.exists():
                    for stale in lanes_root.glob(f"lane-{self.session_id}-*"):
                        shutil.rmtree(stale, ignore_errors=True)
                    with contextlib.suppress(OSError):
                        lanes_root.rmdir()  # 非空（他場活 lane）會拋 OSError → 保留
        return result

    async def _run(self, requirement: str) -> dict:
        """開工：建主 lane、廣播 SESSION_STARTED、git init，再交由 workflow 直譯器派發各
        stage。預設 workflow（default_workflow）走的 handler 與重構前同一段碼、同一順序，
        故與舊行為等價（既有測試套件為等價 oracle）。回傳結果摘要供 autopilot 使用。
        """
        self._requirement = requirement
        experts = self._get_experts()
        # 主（循序）lane：cwd/experts 即 session 本身。逐任務迭代與其 helper 全走它，
        # 行為與重構前逐字等價；並行模式（後續階段）才會另建隔離 lane。
        self._main_ctx = LaneContext(
            "main", self.cwd, experts, self._critics, last_commit=self._last_commit
        )
        self._lane_ctxs.append(self._main_ctx)

        role_providers = self._role_provider_map(experts)  # 各成員綁的 provider（混合模式可視化）
        await self.broadcast(
            events.StudioEvent(
                events.EventType.SESSION_STARTED,
                self.session_id,
                {
                    "requirement": requirement,
                    "repo_url": self._repo_url,
                    "base_repo": self._base_repo or None,
                    "workspace_id": self.workspace_id,
                    # 以實際建立的專家為準（offline 顯示 4 位、正式顯示全部）。
                    # provider：混合模式下各成員實際綁的 provider，前端據此標示「誰用哪家額度」。
                    "roster": [
                        {
                            "key": ex.role.key,
                            "name": ex.role.name,
                            "avatar": ex.role.avatar,
                            "title": ex.role.title,
                            "tags": ex.role.tags,
                            "provider": role_providers.get(ex.role.key, ""),
                        }
                        for ex in experts.values()
                    ],
                },
            )
        )
        if self.cwd:
            await runner.git_init(self.cwd)

        # 動態流程定義快照：開場廣播本場採用的 workflow 名稱與 stage 序列，供前端呈現流程
        # 地圖、入 history 供重播。每筆 stage 帶 type 與顯示名（缺省用 type）。
        await self.broadcast(
            events.workflow_plan(
                self.session_id,
                self._workflow.get("name", ""),
                [
                    {"type": s["type"], "name": s.get("name") or s["type"]}
                    for s in self._workflow["stages"]
                ],
            )
        )

        await self._run_workflow()

        return {
            "completed": self._done,
            "incomplete_reason": self._incomplete_reason,
            "shippable": self._shippable,
            "followups": self._followups,
            "followup_items": self._followup_items,
            "core_changes": self._core_changes,
            "commit": self._last_commit,
            "vision": self._vision,
        }

    # --- 動態流程直譯器 ------------------------------------------------
    async def _run_workflow(self) -> None:
        """按 self._workflow 的 stages 順序派發 stage handler。

        刻意「不」在 stage 之間插入頂層 self._stop 檢查——重構前 _run 也是一路走到底
        （各 stage 內部各自尊重 _stop／_should_wind_down，如 _run_waves／_wrap_up），
        Demo／驗收／發佈在被中止後仍照常收尾。維持此控制流即與舊行為等價。
        """
        for stage in self._workflow["stages"]:
            await self._dispatch_stage(stage)

    def _when_ok(self, when: str) -> bool:
        """評估 stage 的 when 條件 token。空＝恆真。

        ``has:<role_key>`` → 該角色本場在場（取代重構前 `if researcher:`／`if devops:`）；
        ``flag:<CONFIG_NAME>`` → config 的同名旗標為真。未知前綴一律放行（不擋）。
        """
        if not when:
            return True
        kind, _, arg = when.partition(":")
        if kind == "has":
            return arg in self._get_experts()
        if kind == "flag":
            return bool(getattr(config, arg, False))
        return True

    async def _dispatch_stage(self, stage: dict) -> None:
        """派發單一 stage：先評估 when（不滿足即跳過），再呼叫對應 _stage_<type> handler。"""
        when = stage.get("when", "")
        if when and not self._when_ok(when):
            return
        handler = getattr(self, f"_stage_{stage['type']}", None)
        if handler is None:
            log.warning("未知 stage type %r，略過", stage.get("type"))
            return
        await handler(stage)

    # --- stage handlers（重構自 _run，搬碼不改邏輯；中間產物寫上 self 黑板）-----
    async def _stage_clarify(self, stage: dict) -> None:
        # 需求澄清（互動 session 限定）：模糊需求先反問，逾時按假設續行，絕不卡流程。
        pm = self._get_experts()["pm"]
        self._clarify_note = await self._clarify_requirement(pm, self._requirement)

    async def _stage_research(self, stage: dict) -> None:
        # 調研（研究員上網查資料，供拆解與設計參考）。過往場次的調研先注入：沿用既有結論、
        # 只查缺口——既省 token 也讓知識跨場次累積（專案模式）。研究員缺席則整段跳過。
        researcher = self._get_experts().get("researcher")
        if not researcher:
            return
        requirement = self._requirement
        await self.broadcast(events.phase_change(self.session_id, "調研", "研究員正在查資料"))
        prior_research = self._knowledge_tail("RESEARCH.md")
        prior_note = (
            f"【既有調研（docs/RESEARCH.md，過往場次累積）】\n{prior_research}\n\n"
            "以上是過往調研結論：請先沿用、只查缺口，不要重查已有答案的問題。\n\n"
            if prior_research
            else ""
        )
        self._research_notes = await researcher.speak(
            self._clarify_note
            + prior_note
            + f"團隊即將開發以下需求，請先上網調研以提供決策依據：\n\n{requirement}\n\n"
            "查可用套件/函式庫、官方 API 與文件、最佳實踐與常見坑，精簡彙整並附來源。",
            self.broadcast,
        )
        # 調研結論沉澱成交付物，下場開場讀回（檔案不存在時 read 回空字串、零行為差）。
        self._persist_knowledge("RESEARCH.md", self._research_notes)
        await self._commit(self._main_ctx, "知識沉澱：調研結論寫入 docs/RESEARCH.md")

    async def _stage_decompose(self, stage: dict) -> None:
        # 拆解：PM 產出結構化任務＋議程，解析任務/依賴邊/議程分派，廣播快照並 commit。
        experts = self._get_experts()
        pm = experts["pm"]
        requirement = self._requirement
        await self.broadcast(events.phase_change(self.session_id, "需求拆解", "PM 正在拆解需求"))
        if self._repo_url:
            repo_note = (
                "我們要在一個現有的 GitHub 專案上工作，原始碼已 clone 到你的工作目錄"
                f"（{self._repo_url}）。請先用工具瀏覽現有結構與檔案，再依需求拆解任務。\n\n"
            )
        elif self._base_repo:
            repo_note = (
                f"這是長期專案，工作目錄裡是目標 GitHub repo（{self._base_repo}）的既有程式碼，"
                f"已同步到其 {config.PUBLISH_BASE} 分支。請先用工具瀏覽既有結構與檔案再拆解任務；"
                "改動要與既有架構一致，在現有程式碼上修改，不要砍掉重練。\n\n"
            )
        else:
            repo_note = ""
        research_note = (
            f"研究員的調研結論供參考：\n{self._research_notes}\n\n" if self._research_notes else ""
        )
        if not research_note:
            # 研究員缺席（離線或被關閉）時，過往場次的調研沉澱仍可供 PM 參考。
            prior_research = self._knowledge_tail("RESEARCH.md")
            if prior_research:
                research_note = (
                    f"過往場次的調研結論（docs/RESEARCH.md）供參考：\n{prior_research}\n\n"
                )
        # 既有改善計畫讀回：把過往場次檢討沉澱的後續改善任務／教訓（docs/IMPROVEMENT.md，跨場次
        # 累積）注入規劃，讓「改善計畫」被下一場行動消化——閉合「驗證→改善計畫→行動」迴圈。
        # 一次性 session（每場新 workspace）首跑無此檔→空字串、零行為差；專案模式才跨場累積生效。
        prior_improvement = self._knowledge_tail("IMPROVEMENT.md")
        improvement_note = (
            "過往場次的改善計畫（docs/IMPROVEMENT.md，尚待消化的改善建議與教訓）供規劃參考，"
            f"請優先納入仍適用者：\n{prior_improvement}\n\n"
            if prior_improvement
            else ""
        )
        # 額度感知派工：拆解前查一次 provider 額度快照，讓 PM 依「目前各家額度」把任務
        # 分散到各 provider（`派工:` 行只是建議，合法性與受限與否由 flow.choose_dispatch 兜底）。
        # 績效感知：另附各 AI 近期考核摘要（studio/appraisal 聚合；無資料/失敗＝空字串）。
        # auto 派工模式（哨兵檔）：PM 全權——provider 限 AUTO_DISPATCH_PROVIDERS 兩家、
        # 每任務必標派工、模型自由指定直通；兜底只剩「該家不可用或用量 ≥ 門檻才改派另一家」。
        await self._refresh_quota_snapshot()
        self._dispatch_auto = config.dispatch_auto()
        if self._dispatch_auto:
            allowed = self._allowed_models()
            known = "；".join(
                f"{p}：{'、'.join(allowed.get(p) or ()) or '（無參考清單）'}"
                for p in config.AUTO_DISPATCH_PROVIDERS
            )
            dispatch_note = (
                self._quota_note(experts)
                + self._appraisal_note(await self._appraisal_perf())
                + "本場為 auto 派工模式，由你全權派工：請為**每個任務**都加一行 "
                "`派工: #<id> <provider> <model>`（provider 限 "
                + "、".join(config.AUTO_DISPATCH_PROVIDERS)
                + "；model 必填，可自由指定任何模型 ID、將直通該家）。請依上方即時額度把任務"
                "分散到兩家、並依各角色任務的難度挑合適模型；注意「模型限額」（如 Fable 的"
                "專屬週限）與全域額度互相獨立——某模型限額吃緊時把任務改用同家其他模型"
                "（如 claude-opus-4-8）即可，不必整家避開；僅當你指定的家不可用或用量 ≥ "
                f"{config.AUTO_DISPATCH_THRESHOLD:.0f}% 時，系統才會改派另一家（此時模型改用"
                f"該家預設）。目前已知可用模型（僅供參考、非白名單）：{known}。\n"
            )
        else:
            dispatch_note = (
                self._quota_note(experts)
                + self._appraisal_note(await self._appraisal_perf())
                + "每個任務可（選填）加一行 `派工: #<id> <provider> [<model>]`（provider 限 "
                + "、".join(config.PROVIDERS)
                + "；model 可省略＝該家預設模型），依上方額度把任務分散到各 provider、避開受限者；"
                "未標派工的任務由系統依額度自動分派。\n"
            )
        pm_plan = await pm.speak(
            (await self._human_prefix())
            + lessons.context(requirement=requirement)  # 教訓庫（按需求相關性挑選；停用時空字串）
            + adr.context(self.cwd)  # 既有架構決策（停用/無 cwd/空白時為空字串）
            + repo_note
            + self._clarify_note
            + research_note
            + improvement_note
            + f"使用者的產品需求如下：\n\n{requirement}\n\n"
            "請拆解成結構化任務清單與驗收標準，並宣告執行指令。\n"
            + AGENDA_PROMPT_RULES.format(keys=", ".join(experts.keys()))
            + dispatch_note,
            self.broadcast,
        )
        self._pm_plan = pm_plan
        self._run_command = runner.parse_run_command(pm_plan)
        self._demo_url = runner.parse_demo_url(pm_plan)
        # PM 的派工建議（無派工行＝空 dict）；消費端在 _work_task 開工前 choose_dispatch。
        self._dispatch_hints = flow.parse_dispatch(pm_plan)
        if config.PARALLEL_TASKS_ENABLED:
            # 並行：解析任務 + 依賴邊，供拓撲分波。
            self._tasks, self._edges = parse_tasks_with_deps(pm_plan)
        else:
            self._tasks = [
                {"id": i, "title": t, "status": "todo"}
                for i, t in enumerate(parse_tasks(pm_plan), start=1)
            ]
            self._edges = []
        # 議程：子題＋主責解析。assignee 硬驗證——合法集合＝本場實際出席角色 keys，
        # 非法/缺漏 fallback engineer（engineer 缺席則第一個出席者），修正記 log（在
        # validate_assignees 內）；絕不讓 LLM 即興分派直通。新 API 一律 from studio.flow。
        self._agenda, self._agenda_corrections = flow.validate_assignees(
            flow.parse_agenda(pm_plan, requirement=requirement),
            list(experts.keys()),
            fallback="engineer",
        )
        # 拆解結果快照（議程、任務、分派表＋修正紀錄）經 broadcast→record_event 入
        # history，供事後重看；前端對 agenda_plan 有對應 case（重播可見）。
        await self.broadcast(
            events.agenda_plan(
                self.session_id,
                self._agenda,
                self._tasks,
                [
                    {"index": i, "title": a["title"], "assignee": a["assignee"]}
                    for i, a in enumerate(self._agenda, start=1)
                ],
                corrections=self._agenda_corrections,
                edges=self._edges,
            )
        )
        await self._board()
        await self._commit(self._main_ctx, "PM 規劃：建立任務清單與驗收標準")

    async def _stage_discuss(self, stage: dict) -> None:
        # 架構：有架構師則由其主導設計決策，否則維持工程師⇄高級工程師辯論。既有決策的注入
        # 與定案沉澱由 ADR 模組負責（_architecture_decision／_debate 內的 adr.context＋record）。
        # 客製 workflow 若明指 roles，覆蓋既有選角（沿用 stage['roles']）；缺省＝既有路徑。
        experts = self._get_experts()
        engineer, senior = experts["engineer"], experts["senior"]
        architect = experts.get("architect")
        requirement = self._requirement
        pm_plan = self._pm_plan
        topic = f"我們要實作這個需求：{requirement}\n任務清單：\n{pm_plan}"
        if self._group:
            # 使用者選定討論小組：以小組成員＋小組 mode 逐子題討論（優先於架構師/預設路徑）。
            self._design_note = await self._discuss_agenda(experts, engineer, senior, requirement)
        elif architect:
            self._design_note = await self._architecture_decision(
                architect, engineer, senior, topic, self._research_notes
            )
        elif config.DISCUSS_MODE in ("round_robin", "parallel"):
            # 引擎模式：逐子題以 topic 餵 DiscussionEngine（引擎介面不動）；各子題結論
            # 串接成 design_note，ADR 蒸餾/commit 收斂為一次（_discuss_agenda 內）。
            self._design_note = await self._discuss_agenda(experts, engineer, senior, requirement)
        else:
            await self._debate(engineer, senior, topic=topic, rounds=config.DEBATE_ROUNDS)

    async def _stage_build(self, stage: dict) -> None:
        # 逐任務迭代：依設定走「波次並行」或循序，兩者共用同一條波次主迴圈。
        # 供每個任務實作時參考的脈絡（澄清 + 調研 + 設計決策）。
        context = ""
        if self._clarify_note:
            context += f"\n{self._clarify_note}"
        if self._research_notes:
            context += f"\n【研究員調研】\n{self._research_notes}\n"
        if self._design_note:
            context += f"\n【架構決策】\n{self._design_note}\n"
        self._all_ok = await self._run_waves(self._pm_plan + context)
        if self._deadline_hit:
            # 撞硬 timeout／用量上限前主動收斂：已完成的續走 Demo/出貨，未動的下面記成 known-limit。
            phase, detail = (
                (
                    "用量預算收斂",
                    "接近 token／成本上限，停止派發新任務，以已完成成果優雅收尾出貨（未完成記為已知限制）。",
                )
                if self._budget_hit
                else (
                    "時間預算收斂",
                    "接近時間上限，停止派發新任務，以已完成成果優雅收尾出貨（未完成記為已知限制）。",
                )
            )
            await self.broadcast(events.phase_change(self.session_id, phase, detail))

    async def _stage_integrate(self, stage: dict) -> None:
        # 整合驗證（維運：裝相依、設環境、跑整合/啟動驗證）。維運缺席則整段跳過。
        devops = self._get_experts().get("devops")
        if not devops:
            return
        await self.broadcast(
            events.phase_change(self.session_id, "整合驗證", "維運工程師驗證整合與環境")
        )
        await devops.speak(
            "請確保整體成果能在乾淨環境跑起來：安裝相依、設定必要環境、實際啟動或跑整合測試，"
            f"並回報結果。整體計畫供參考：\n{self._pm_plan}",
            self.broadcast,
        )

    async def _stage_demo(self, stage: dict) -> None:
        # 最終 Demo（實際執行整體產出）。
        self._demo = await self._final_demo()

    async def _stage_wrap_up(self, stage: dict) -> None:
        # PM 驗收 + 檢討。客觀閘門開啟時，Demo「實際執行」未通過則整體不予驗收——PM 仍照常
        # 發言檢討，但 `決議: 完成` 翻轉不了真實失敗的 Demo（只在 Demo 真的有跑且失敗時否決，
        # 無 demo 指令不在此誤殺）。
        pm = self._get_experts()["pm"]
        demo = self._demo
        demo_veto = config.objective_gate_enabled() and demo is not None and not demo.ok
        if demo_veto:
            await self.broadcast(
                events.phase_change(
                    self.session_id, "客觀閘門", "最終 Demo 實際執行未通過，整體不予驗收"
                )
            )
        self._done = await self._wrap_up(pm, self._all_ok and not demo_veto, demo_veto=demo_veto)

        # 可帶「已知限制」出貨：把「全有全無」放寬為「核心客觀證據通過即發佈」。未過的次要任務
        # 記成已知限制（寫進交付物＋回填 backlog），不再讓單一 flaky 小任務永久擋住整個可用
        # 產品的交付。安全護欄在 shippable_verdict：沒跑過 Demo（無客觀證據）又非全過時不出貨。
        # 完整完成（done）與可出貨（shippable）分流，completed 仍據實回報。
        core_verified = demo is not None and demo.ok
        self._shippable = shippable_verdict(
            all_ok=self._all_ok,
            demo_veto=demo_veto,
            core_verified=core_verified,
            stopped=self._stop,
        )
        unmet = [t for t in self._tasks if t.get("status") != "done"]
        if self._shippable and not self._done and unmet:
            await self._record_known_limitations(unmet)

    async def _stage_publish(self, stage: dict) -> None:
        # 視設定自動發佈成果到 GitHub（此時專家團隊仍在線，可在 CI 失敗時修正）。
        engineer = self._get_experts()["engineer"]
        await self._maybe_publish(self._shippable, engineer)

    def _dynamic_blackboard(self) -> str:
        """組給 PM 動態決策參考的「黑板」摘要：已完成 stage 的關鍵中間產物。"""
        parts: list[str] = []
        if self._clarify_note:
            parts.append(self._clarify_note.strip())
        if self._design_note:
            parts.append(f"【架構決策】\n{self._design_note.strip()}")
        if self._pm_plan:
            parts.append(f"【任務計畫】\n{self._pm_plan.strip()}")
        return "\n\n".join(parts)

    # --- 額度感知分派 + 動態招募（dynamic stage 共用）------------------------
    def _role_provider_map(self, experts: dict) -> dict[str, str]:
        """本場各在場角色實際綁的 provider（混合模式）。

        per-task 派工的暫時綁定最優先（`_dispatch_bindings`，任務期間實作者換綁到別家）、
        其次招募成員的實際綁定（`_recruit_providers`，可能因額度受限自動重綁／PM 指定而異於
        角色預設）；其餘走 `effective_provider`。讓額度摘要/roster「誰用哪家額度」顯示正確。
        """
        from .providers import effective_provider

        return {
            k: self._dispatch_bindings.get(k)
            or self._recruit_providers.get(k)
            or effective_provider(ex.role)
            for k, ex in experts.items()
        }

    async def _refresh_quota_snapshot(self) -> None:
        """動態 stage 開頭查一次 provider 額度快照（asyncio.to_thread；失敗存 None）。

        snapshot 內各 usage 模組 60s 快取＋未設定 provider 不打外網，故成本低；存於 self 供本
        stage 的 PM 摘要與招募自動重綁共用，不每 hop 重查。
        """
        try:
            self._quota_snap = await asyncio.to_thread(provider_quota.snapshot)
        except Exception:  # noqa: BLE001 — 額度查詢失敗不該拖垮討論，退回「無額度資訊」
            self._quota_snap = None

    def _auto_quota_snap(self, snap: dict | None) -> dict | None:
        """auto 派工模式：把額度快照過濾到 AUTO_DISPATCH_PROVIDERS 兩家子集。

        供 PM 額度摘要與招募重綁共用——PM 看不到兩家以外的額度、重綁也不會落到子集外，
        與 per-task 派工的 digest 過濾同一道理。手動模式呼叫端不經過此函式。
        """
        if not snap:
            return snap
        return {
            **snap,
            "providers": [
                e
                for e in snap.get("providers", [])
                if isinstance(e, dict) and e.get("key") in config.AUTO_DISPATCH_PROVIDERS
            ],
        }

    def _quota_note(self, experts: dict) -> str:
        """把 provider 即時額度摘要組成給 PM 的 prompt 片段（無快照/空摘要回空字串）。"""
        if not self._quota_snap:
            return ""
        snap = self._auto_quota_snap(self._quota_snap) if self._dispatch_auto else self._quota_snap
        summary = provider_quota.summarize_for_pm(snap, self._role_provider_map(experts))
        if not summary:
            return ""
        return (
            "各成員所用 provider 目前額度（混合模式每家不同，分派/招募時優先用還有額度的、"
            "避開受限者）：\n" + summary + "\n\n"
        )

    # --- 考核（Appraisal）：績效聚合注入與 per-task 客觀指標收集 ----------------
    async def _appraisal_perf(self) -> dict:
        """近期考核聚合快照（appraisal.summary；asyncio.to_thread）；停用/失敗一律回 {}。

        兩個消費端共用：PM 拆解 prompt 的 _appraisal_note 摘要、per-task 派工
        choose_dispatch 的 performance 次序鍵（providers 層的 avg_score）。查詢失敗
        容錯回空 dict——考核是旁路觀測，絕不拖垮拆解與派工。
        """
        if not config.APPRAISAL_ENABLED:
            return {}
        try:
            return await asyncio.to_thread(appraisal.summary) or {}
        except Exception:  # noqa: BLE001 — 考核聚合失敗不得影響拆解/派工
            log.warning("考核聚合查詢失敗（略過，回空摘要）", exc_info=True)
            return {}

    def _appraisal_note(self, summ: dict) -> str:
        """把近期考核聚合組成給 PM 的 prompt 片段（同 _quota_note 樣式；無資料回空字串）。

        例：「各 AI 近期考核（平均分 1–5，分派時可偏好表現好的）：claude 4.5（12 件，
        通過率 92%）、codex 3.8（5 件）」。pass_rate 無客觀樣本（None）時省略該段。
        """
        provs = (summ or {}).get("providers") or {}
        parts: list[str] = []
        for prov, st in provs.items():
            if not isinstance(st, dict) or st.get("avg_score") is None:
                continue
            seg = f"{prov} {st['avg_score']}（{st.get('n', 0)} 件"
            if st.get("pass_rate") is not None:
                seg += f"，通過率 {round(st['pass_rate'] * 100)}%"
            seg += "）"
            parts.append(seg)
        if not parts:
            return ""
        return "各 AI 近期考核（平均分 1–5，分派時可偏好表現好的）：" + "、".join(parts) + "\n\n"

    def _collect_task_perf(
        self, ctx: LaneContext, task: dict, impl_role: str, duration_s: float
    ) -> None:
        """任務結束（還原換綁前）收客觀指標：實際 provider 綁定與耗時，併入本場考核暫存。

        qa_rounds/qa_passed/senior_approved 由 _work_task_rounds 逐輪寫入；此處補
        「該任務實作者實際綁定的 provider」（per-task 派工換綁尚未還原，_role_provider_map
        會先看 _dispatch_bindings）與累計耗時（huddle 重試再進來時累加）。model 僅在
        per-task 派工顯式指定時已由 _dispatch_task_expert 記下，取不到＝None。
        永不 raise——考核是旁路觀測，不得影響任務主流程。
        """
        try:
            perf = self._task_perf.setdefault(task["id"], {})
            perf.setdefault("qa_rounds", 0)
            perf.setdefault("qa_passed", None)
            perf.setdefault("senior_approved", None)
            perf.setdefault("model", None)
            perf.setdefault("input_tokens", None)
            perf.setdefault("output_tokens", None)
            perf.setdefault("total_tokens", None)
            perf.setdefault("cost_usd", None)
            perf.setdefault("cost_source", None)
            perf["role"] = impl_role
            expert = ctx.experts.get(impl_role)
            provider = ""
            if expert is not None:
                provider = self._role_provider_map({impl_role: expert}).get(impl_role, "")
                # 模型可見性：未經 per-task 派工顯式指定時，改問實際綁定的專家「生效模型」
                # （各 Expert 類的 effective_model()；測試 stub 無此方法＝維持 None）。
                if not perf.get("model"):
                    fn = getattr(expert, "effective_model", None)
                    if callable(fn):
                        perf["model"] = fn() or None
            perf["provider"] = provider or perf.get("provider") or ""
            perf["duration_s"] = round((perf.get("duration_s") or 0.0) + duration_s, 1)
        except Exception:  # noqa: BLE001 — 考核收集失敗不得拖垮任務
            log.warning("任務 #%s 考核指標收集失敗（略過）", task.get("id"), exc_info=True)

    def _allowed_models(self) -> dict[str, tuple[str, ...]]:
        """各 provider 的模型白名單（settings 常數）——orchestrator 可 import settings、flow 不可。

        choose_dispatch／招募用它兜底 PM 給的 model：不在對應 provider 白名單即棄用、
        沿用該家預設模型槽，絕不把 LLM 即興發明的模型 ID 直通到 provider。
        """
        from . import settings

        return {
            "claude": settings.CLAUDE_MODELS,
            "codex": settings.CODEX_MODELS,
            "minimax": settings.MINIMAX_MODELS,
            "antigravity": settings.ANTIGRAVITY_MODELS,
        }

    async def _dispatch_task_expert(self, ctx: LaneContext, task: dict, impl_role: str):
        """任務開工前的額度感知 per-task 派工：暫時把實作者換綁到最有額度的 provider/model。

        決策是純函式 flow.choose_dispatch（PM `派工:` 建議優先、受限跳開、同分分攤避開剛用過
        的），本方法只做副作用：查額度快照、以（_dispatch_factory 或 providers.make_expert）建
        臨時專家換綁 ctx.experts[impl_role]、記綁定/序列、廣播 dispatch_decision。回傳「還原
        callback」，呼叫端在 finally 執行：還原原專家、還原綁定、best-effort stop 臨時專家。

        安全邊界（不換綁、回 no-op）：呼叫端顯式注入 experts（測試 stub／離線假專家）且未注入
        _dispatch_factory、離線模式、無 cwd、快照查詢失敗、choose 回空 provider（全掛）、或選擇
        與現綁定相同且無模型覆寫——以上皆完全沿用既有行為。臨時專家建立失敗亦不拖垮任務。
        """

        async def _noop() -> None:
            return None

        prev_expert = ctx.experts.get(impl_role)
        if prev_expert is None:
            return _noop
        if self._dispatch_factory is None and (
            self._experts_injected or ctx.cwd is None or config.OFFLINE_MODE
        ):
            return _noop
        await self._refresh_quota_snapshot()
        if not self._quota_snap:
            return _noop
        # 績效感知：近期考核的 {provider: avg_score} 作 choose_dispatch 同分次序鍵
        # （偏好歷史表現高者）；聚合失敗回 {} ＝ 行為與純額度分派相同。
        perf_summary = await self._appraisal_perf()
        digest = provider_quota.digest(self._quota_snap)
        if self._dispatch_auto:
            # auto 派工：候選夾到兩家子集（PM 違規指定他家＝hint 不在 digest、自然兜底），
            # 門檻放寬到 AUTO_DISPATCH_THRESHOLD、模型直通不查白名單（model_free）。
            digest = {k: v for k, v in digest.items() if k in config.AUTO_DISPATCH_PROVIDERS}
        choice = flow.choose_dispatch(
            digest,
            task,
            self._dispatch_hints.get(task["id"]) or {},
            self._allowed_models(),
            list(self._dispatch_recent),
            performance={
                p: st.get("avg_score") or 0.0
                for p, st in (perf_summary.get("providers") or {}).items()
                if isinstance(st, dict)
            },
            threshold=config.AUTO_DISPATCH_THRESHOLD if self._dispatch_auto else 90.0,
            model_free=self._dispatch_auto,
        )
        provider, model = choice.get("provider", ""), choice.get("model", "")
        current = self._role_provider_map({impl_role: prev_expert}).get(impl_role, "")
        # 全掛（空 provider）→ 沿用原綁定；與現綁定同家且無模型覆寫 → 不必換（零成本路徑）。
        if not provider or (provider == current and not model):
            return _noop
        if self._dispatch_factory is not None:
            factory = self._dispatch_factory
        else:
            from .providers import make_expert

            factory = make_expert
        try:
            temp = factory(
                prev_expert.role,
                f"{self.session_id}:task{task['id']}",
                ctx.cwd,
                provider=provider,
                model=model,
            )
        except Exception:  # noqa: BLE001 — 換綁失敗不得拖垮任務，沿用原綁定
            log.exception("任務 #%s 派工建立 %s 專家失敗，沿用原綁定", task["id"], provider)
            return _noop
        prev_binding = self._dispatch_bindings.get(impl_role)
        ctx.experts[impl_role] = temp
        self._dispatch_bindings[impl_role] = provider  # 併入 _role_provider_map（額度摘要正確）
        self._dispatch_recent.append(provider)
        # 考核暫存：記下本任務實際換綁的 provider/model（_collect_task_perf 併入客觀指標；
        # model 空字串＝該 provider 預設模型槽，考核紀錄以 None 表「未知/未指定」）。
        perf = self._task_perf.setdefault(task["id"], {})
        perf["provider"], perf["model"] = provider, model or None
        await self.broadcast(
            events.dispatch_decision(
                self.session_id,
                task["id"],
                task.get("title", ""),
                impl_role,
                provider,
                model,
                choice.get("reason", ""),
                mode="auto" if self._dispatch_auto else "manual",
            )
        )

        async def _restore() -> None:
            ctx.experts[impl_role] = prev_expert
            if prev_binding is None:
                self._dispatch_bindings.pop(impl_role, None)
            else:
                self._dispatch_bindings[impl_role] = prev_binding
            try:
                await temp.stop()  # best-effort：臨時專家的子程序/連線不可洩漏
            except Exception:  # noqa: BLE001
                log.warning("任務 #%s 臨時專家 stop 失敗", task["id"], exc_info=True)

        return _restore

    def _build_liquid_role(self, spec: dict) -> Role | None:
        """從 PM 的 `招募:` 規格現場液生一個臨時 Role；key 不合法/與既有衝突/缺專長回 None。"""
        from .role_store import KEY_RE
        from .roles import _COMMON

        key = (spec.get("key") or "").strip()
        expertise = (spec.get("expertise") or "").strip()
        if not KEY_RE.match(key) or key in BY_KEY or not expertise:
            return None
        name = (spec.get("name") or key).strip() or key
        body = (
            f"你是{name}，專長：{expertise}。請只就你的專長對任務追加把關或推進，"
            "言簡意賅。\n輸出格式：最後一行明確給出你的結論或決議。"
        )
        return Role(
            key=key,
            name=name,
            avatar="🆕",
            title=expertise[:24],
            model=config.MODEL_FAST,
            allowed_tools=["Read", "Grep"],
            permission_mode="default",
            system_prompt=_COMMON + "\n" + body,
            tags=["液生招募"],
            description=expertise,
        )

    def _pick_provider(self, role: Role, hint: str) -> str:
        """招募綁定 provider：PM 明指（hint）優先，否則角色有效 provider；受限則自動重綁最寬鬆就緒者。"""
        from .providers import effective_provider

        prov = (hint or "").strip() or effective_provider(role)
        if prov not in config.PROVIDERS:
            prov = effective_provider(role)
        snap = self._quota_snap
        threshold = provider_quota.CONSTRAINED_THRESHOLD
        if self._dispatch_auto:
            # auto 派工模式：招募綁定也夾到兩家子集、門檻同 per-task 派工放寬。
            snap = self._auto_quota_snap(snap)
            threshold = config.AUTO_DISPATCH_THRESHOLD
            if prov not in config.AUTO_DISPATCH_PROVIDERS:
                alt = provider_quota.least_constrained_ready(snap) if snap else None
                prov = alt or config.AUTO_DISPATCH_PROVIDERS[0]
        if snap and provider_quota.constrained(snap, prov, threshold=threshold):
            alt = provider_quota.least_constrained_ready(snap)
            if alt and alt != prov:
                log.info("招募 %s：provider %s 受限，自動重綁 %s", role.key, prov, alt)
                prov = alt
        return prov

    async def _recruit(
        self, ctx: LaneContext, role: Role, provider_hint: str, reason: str, model: str = ""
    ) -> str:
        """把一位新角色建成 expert 加入 lane（額度感知綁 provider），廣播 EXPERT_JOINED。回 role_key。

        加進 ctx.experts 即被 run() finally 的回收涵蓋（不另登記）。測試以 self._recruit_factory
        注入 stub（簽名 (role, cwd, provider) 不變）。達 RECRUIT_MAX 上限的把關在呼叫端
        （_resolve_or_recruit）。``model``＝PM `模型:` 行指定的模型（選填）：僅在屬於實際綁定
        provider 的白名單時傳給 make_expert，否則棄用、沿用該家預設模型槽。
        """
        prov = self._pick_provider(role, provider_hint)
        if model:
            if self._dispatch_auto and prov in config.AUTO_DISPATCH_PROVIDERS:
                # auto 派工：模型直通不查白名單，但僅限實際綁定＝PM 指定的家
                # （被重綁到另一家時棄用，避免 A 家模型 ID 直通 B 家）。
                if prov != (provider_hint or "").strip().lower():
                    model = ""
            elif model not in (self._allowed_models().get(prov) or ()):
                model = ""  # 模型不屬於實際綁定 provider 的白名單（或 provider 被重綁）→ 棄用
        if self._recruit_factory is not None:
            expert = self._recruit_factory(role, ctx.cwd, prov)
        else:
            from .providers import make_expert

            expert = make_expert(role, self.session_id, ctx.cwd, provider=prov, model=model)
        ctx.experts[role.key] = expert
        self._recruit_providers[role.key] = prov  # 記實際綁定，供額度摘要/roster 正確顯示
        self._recruited += 1
        await self.broadcast(
            events.expert_joined(
                self.session_id,
                role.key,
                role.name,
                role.avatar,
                role.title,
                list(role.tags),
                prov,
                reason,
            )
        )
        return role.key

    async def _resolve_or_recruit(self, ctx: LaneContext, step: dict, fallback: str) -> str:
        """把 PM 的下一步解析成「可發言的 role_key」。

        `下一步:` 是「找誰」的權威：在場→直接用；否則在招募上限內——若 PM 同時給 `招募:` 規格
        且其 key 與 `下一步:` 一致（或 PM 只給招募、無下一步）→液生 persona 招募；role 在庫(BY_KEY)
        →庫招募。都不行（達上限/非法/招募 key 與下一步不符）→ flow.validate_assignees 兜底。
        """
        experts = ctx.experts
        role = (step.get("role") or "").strip()
        provider = step.get("provider", "")
        model = (step.get("model") or "").strip()  # PM `模型:` 行（選填；白名單兜底在 _recruit）
        if role in experts:
            return role
        if self._recruited < config.RECRUIT_MAX:
            spec = step.get("recruit")
            # 液生僅在「無下一步」或「下一步＝招募 key」時觸發，避免遮蔽明指的 `下一步:`。
            if spec and (not role or (spec.get("key") or "").strip() == role):
                liquid = self._build_liquid_role(spec)
                if liquid is not None:
                    return await self._recruit(ctx, liquid, provider, "液生招募", model)
            if role in BY_KEY:
                return await self._recruit(ctx, BY_KEY[role], provider, "庫招募", model)
        fixed, _ = flow.validate_assignees(
            [{"assignee": role}], list(experts.keys()), fallback=fallback
        )
        return fixed[0]["assignee"]

    async def _stage_dynamic(self, stage: dict) -> None:
        """動態 step：PM 逐 hop 在運行時決定下一個發言角色（或結束），有界迴圈防無限。

        防呆全部沿用既有範式：budget 硬上限 hop 數（對齊 TASK_MAX_ROUNDS／CRITIC_MAX_REJECTS
        收斂預算）；每圈先檢查 _stop／_should_wind_down 立即優雅結束；解析不出合法角色→以
        flow.validate_assignees 風格 fallback；PM 連續輸出高相似決策→flow.is_stalled 收斂；
        每次發言走 self._speak（號誌節流＋provider-unavailable 穿透，不誤判「未達完成」）。
        預設 workflow 不含此 stage，故不影響等價性。
        """
        self._get_experts()  # 確保主 lane 專家已建立（ctx.experts 即此 dict，可被招募就地擴充）
        ctx = self._main_ctx
        budget = stage.get("budget") or config.DYNAMIC_STEP_BUDGET
        fallback = stage.get("fallback", "engineer")
        name = stage.get("name") or "動態決策"
        await self.broadcast(
            events.phase_change(self.session_id, name, f"PM 運行時決定下一步（最多 {budget} 步）")
        )
        # 額度感知：開頭查一次 provider 額度快照，供 PM 依「目前額度分配」分派/招募。
        await self._refresh_quota_snapshot()
        decisions: list[str] = []
        planning: list[str] = []  # 被指派者的發言內容，收尾沉澱進 design_note 供 build 任務脈絡參考
        for _hop in range(max(budget, 0)):
            if self._stop or self._should_wind_down():
                break
            roster_desc = "\n".join(  # 每圈重算：招募的新人本圈即可被指派
                f"- {key}: {ex.role.name}（{ex.role.description or ex.role.title}）"
                for key, ex in ctx.experts.items()
            )
            # 已完成的分派與回應餵回 PM，讓它依成員實際回應「接著協調」而非每步盲分派（加強溝通/分派）。
            planning_note = (
                "目前為止的分派與回應（請據此協調下一步、避免重複指派）：\n"
                + "\n\n".join(planning)
                + "\n\n"
                if planning
                else ""
            )
            decision = await self._speak(
                ctx,
                "pm",
                f"目前進度摘要：\n{self._dynamic_blackboard()}\n\n"
                + planning_note
                + self._quota_note(ctx.experts)
                + f"目前團隊成員（role_key）：\n{roster_desc}\n\n"
                f"需求：{self._requirement}\n\n"
                "請判斷為了把這個需求推進到可交付，下一步該找誰做什麼，輸出：\n"
                "`下一步: <role_key>`（現有成員，或庫裡角色 key 以招募之）\n"
                "`指示: <要請該成員具體做什麼>`\n"
                "若現有成員都不適合，可現場招募新人：加 `招募: <key> | <名稱> | <一句專長>` "
                "並用 `下一步: <key>` 指該新人；混合模式可選 "
                "`provider: <claude|codex|minimax|antigravity>` 指定綁定（額度受限時系統會自動改綁），"
                "另可加 `模型: <model>` 指定該 provider 的模型（非白名單值會被忽略）。\n"
                + self._vote_hint()
                + "若已無需再推進，輸出一行 `下一步: 結束`。",
                None,
            )
            decisions.append(decision)
            # 停滯：PM 連續輸出高相似決策（無實質進展）即收斂（重用既有偵測）。
            if flow.is_stalled(decisions, config.STALL_ROUNDS):
                break
            # 表決 hook（在 parse_next_step 之前攔截）：PM 無法決定、發起 `表決:` → 舉行
            # 3-AI 表決，結果經 planning_note 注入下一 hop 的 PM prompt 脈絡，本 hop 消化完畢。
            vote_note = await self._maybe_hold_vote(ctx, decision, None)
            if vote_note:
                planning.append(vote_note)
                continue
            step = flow.parse_next_step(decision)
            if step["end"]:
                break
            # 解析下一個發言者：在場直接用、庫裡有/PM 液生則招募、否則 validate_assignees 兜底。
            role = await self._resolve_or_recruit(ctx, step, fallback)
            if not role:
                break  # 無任何在場角色（理論上不會發生，pm 必在場）——保底結束。
            instruction = step["instruction"] or "請依目前進度推進這個需求。"
            spoke = await self._speak(ctx, role, instruction, None)
            if (spoke or "").strip():
                planning.append(f"【{ctx.experts[role].role.name}】{spoke.strip()}")
        # 把本階段的動態規劃沉澱進 design_note，讓 build 的逐任務脈絡帶得到——否則 dynamic-first
        # 用 dynamic 取代 discuss 後，PM 的運行時溝通/分派討論會對任務執行「失聯」。累加不覆寫。
        if planning:
            block = "\n【動態規劃與分派】\n" + "\n\n".join(planning)
            self._design_note = (self._design_note + block) if self._design_note else block.lstrip()

    # --- task_pipeline 資料驅動（_work_task 讀 workflow 的 build.task_pipeline）-----
    def _build_task_pipeline(self) -> list[dict]:
        """取目前 workflow 的 build stage 的 task_pipeline（無 build／無 pipeline 時回 []）。"""
        for stage in self._workflow.get("stages", []):
            if stage.get("type") == "build":
                return stage.get("task_pipeline", [])
        return []

    def _task_review_role_keys(self) -> set[str]:
        """task_pipeline 的 review stage 指定的 reviewer 角色集合（其 gate 列出的 role）。

        無 task_pipeline／無 review stage 時回預設 ``{qa, senior, security}``（重現今日行為）。
        本增量用它決定「security 是否參與審查」；qa／senior 為核心必審（沿用既有裁決聚合）。
        """
        for stage in self._build_task_pipeline():
            if stage.get("type") == "review":
                keys = {g.get("role") for g in stage.get("gate", []) if g.get("role")}
                return keys or {"qa", "senior", "security"}
        return {"qa", "senior", "security"}

    def _task_critic_enabled(self) -> bool:
        """task_pipeline 是否含 critic 閘門（gate stage 且 verdict 為 critic_blocks）。

        無 task_pipeline 資訊時回 True（重現今日：critic 仍由 config.CRITIC_ENABLED 控制）；
        客製 workflow 省略 gate stage → 本場跳過 critic 關卡。
        """
        pipeline = self._build_task_pipeline()
        if not pipeline:
            return True
        return any(
            stage.get("type") == "gate"
            and any(g.get("verdict") == "critic_blocks" for g in stage.get("gate", []))
            for stage in pipeline
        )

    def _task_reviewers(self, experts: dict) -> list[tuple[str, str]]:
        """task_pipeline 的 review stage gate → 有序 ``(role_key, verdict_name)``，過濾在場專家。

        無 task_pipeline／無 review stage／review 無 gate 時回預設
        ``[(qa, qa_passed), (senior, senior_approved), (security, security_approved)]``——
        過濾在場後即「qa/senior 必審＋security 在場才審」，重現今日行為。
        客製 workflow 可在 review gate 增刪 reviewer（含非核心角色＋對應 verdict）。
        """
        default = [
            ("qa", "qa_passed"),
            ("senior", "senior_approved"),
            ("security", "security_approved"),
        ]
        spec: list[tuple[str, str]] | None = None
        for stage in self._build_task_pipeline():
            if stage.get("type") == "review":
                gate = [
                    (g["role"], g["verdict"])
                    for g in stage.get("gate", [])
                    if g.get("role") and g.get("verdict")
                ]
                spec = gate or None
                break
        if spec is None:
            spec = default
        return [(rk, vn) for rk, vn in spec if rk in experts]

    def _task_implementer(self) -> str:
        """task_pipeline 的 implement stage 指定的實作者 role_key（無則預設 engineer）。"""
        for stage in self._build_task_pipeline():
            if stage.get("type") == "implement" and stage.get("assignee"):
                return stage["assignee"]
        return "engineer"

    def _task_max_rounds(self) -> int | None:
        """task_pipeline 的 review stage max_rounds（>0 才覆寫 config.TASK_MAX_ROUNDS）；無則 None。"""
        for stage in self._build_task_pipeline():
            if stage.get("type") == "review" and stage.get("max_rounds"):
                return stage["max_rounds"]
        return None

    def _review_prompt(self, role_key: str, verdict_name: str, task: dict, pm_plan: str) -> str:
        """組 reviewer 的 prompt：已知核心角色用專屬全文（保預設逐字等價），其餘用 verdict generic。"""
        tmpl = _REVIEW_PROMPTS.get(role_key)
        if tmpl is not None:
            return tmpl.format(id=task["id"], title=task["title"], plan=pm_plan)
        instruction = _VERDICT_INSTRUCTION.get(verdict_name, "給出明確決議。")
        return f"請審查任務 #{task['id']}：{task['title']} 的成果，{instruction}"

    def _task_dynamic_stage(self) -> dict | None:
        """task_pipeline 的 dynamic stage（PM 任務內動態追加把關）；無則 None。"""
        for stage in self._build_task_pipeline():
            if stage.get("type") == "dynamic":
                return stage
        return None

    async def _task_dynamic_consult(
        self,
        ctx: LaneContext,
        task: dict,
        review_section: str,
        tag: int | None,
        bc: Broadcast,
    ) -> tuple[bool, str]:
        """task 級 dynamic stage：標準審查通過後，PM 有界地動態追加把關。

        回傳 ``(是否有阻擋異議, 退回 feedback)``。task_pipeline 無 dynamic stage → 立即
        ``(False, "")``＝零行為變更（預設 workflow 不含此 stage）。防呆全部沿用既有範式：
        budget 硬上限／每圈檢查 _stop+_should_wind_down／validate_assignees fallback／
        is_stalled 收斂／_speak（號誌＋provider 穿透）。被追加專家以 critic_blocks 判定異議。
        """
        stage = self._task_dynamic_stage()
        if stage is None:
            return False, ""
        budget = stage.get("budget") or config.DYNAMIC_STEP_BUDGET
        fallback = stage.get("fallback", "engineer")
        await self._refresh_quota_snapshot()  # 額度感知：供 PM 依額度挑追加把關者/招募
        decisions: list[str] = []
        vote_note = ""  # 上一 hop 的表決結果，注入下一 hop 的 PM prompt 脈絡
        for _hop in range(max(budget, 0)):
            if self._stop or self._should_wind_down():
                break
            roster_desc = "\n".join(  # 每圈重算：招募的新人本圈即可被指派
                f"- {k}: {ex.role.name}（{ex.role.description or ex.role.title}）"
                for k, ex in ctx.experts.items()
            )
            decision = await self._speak(
                ctx,
                "pm",
                f"任務 #{task['id']}：{task['title']} 已通過標準審查。審查摘要：\n{review_section}\n\n"
                + (f"{vote_note}\n\n" if vote_note else "")
                + self._quota_note(ctx.experts)
                + f"可追加把關的成員：\n{roster_desc}\n\n"
                "若你認為還需要某位成員（或庫裡角色/現場招募新人）追加把關，輸出：\n"
                "`下一步: <role_key>`\n`指示: <要他確認什麼>`\n"
                "招募新人：加 `招募: <key> | <名稱> | <一句專長>`（可選 `provider: <名稱>`）。\n"
                + self._vote_hint()
                + "若不需要追加、可放行，輸出 `下一步: 結束`。",
                tag,
                token_usage_task_id=task["id"],
            )
            decisions.append(decision)
            if flow.is_stalled(decisions, config.STALL_ROUNDS):
                break
            # 表決 hook（在 parse_next_step 之前攔截）：命中→舉行表決，結果注入下一 hop。
            note = await self._maybe_hold_vote(ctx, decision, tag, token_usage_task_id=task["id"])
            if note:
                vote_note = note
                continue
            step = flow.parse_next_step(decision)
            if step["end"]:
                break
            role = await self._resolve_or_recruit(ctx, step, fallback)
            if not role:
                break
            instruction = step["instruction"] or "請追加把關這個任務的成果。"
            opinion = await self._speak(
                ctx,
                role,
                f"{instruction}\n\n若發現會阻擋交付的實質問題，最後一行輸出 `異議: 成立` 並說明；"
                "否則輸出 `異議: 不成立`。",
                tag,
                token_usage_task_id=task["id"],
            )
            blocks = critic_blocks(opinion)
            await bc(events.critic_review(self.session_id, role, not blocks, opinion))
            if blocks:
                return True, f"【追加把關（{ctx.experts[role].role.name}）退回理由】\n{opinion}"
        return False, ""

    # --- 3-AI 表決（PM 無法決定時跨 provider 多數決）--------------------------
    def _vote_hint(self) -> str:
        """PM dynamic prompt 的表決提示行（表決關閉或達單場上限時回空字串、不誤導 PM）。"""
        if not config.VOTE_ENABLED or self._votes_held >= config.VOTE_MAX:
            return ""
        return (
            "若你無法決定，可發起表決：`表決: <議題> | <選項A> | <選項B>`"
            "（系統將找兩位不同 provider 的 AI 與你多數決）。\n"
        )

    def _build_voter_role(self, provider: str) -> Role:
        """表決投票員的輕量臨時 Role（比照 _build_liquid_role 範式：唯讀工具、不進 roster）。"""
        from .roles import _COMMON

        name = f"表決投票員（{provider}）"
        body = (
            f"你是{name}，負責就 PM 提出的議題獨立投票。請簡短說明理由，"
            "最後一行輸出 `投票: <選項原文>`。"
        )
        return Role(
            key=f"voter_{provider}",
            name=name,
            avatar="🗳️",
            title="表決投票員",
            model=config.MODEL_FAST,
            allowed_tools=["Read", "Grep"],
            permission_mode="default",
            system_prompt=_COMMON + "\n" + body,
            tags=["表決"],
            description="一次性表決投票員",
        )

    async def _maybe_hold_vote(
        self,
        ctx: LaneContext,
        decision: str,
        tag: int | None,
        *,
        token_usage_task_id: int | None = None,
    ) -> str:
        """檢查 PM 決策是否發起表決（`表決:` 行）；命中則舉行並回「表決結果」脈絡字串。

        呼叫端在 flow.parse_next_step 之前先呼叫本方法：回傳非空 → 該 hop 已由表決消化，
        把回傳字串注入下一 hop 的 PM prompt 脈絡。未命中／VOTE_ENABLED 關閉 → 回空字串；
        達 config.VOTE_MAX 單場上限 → 記 log、忽略表決請求（照樣回空字串，不卡流程）。
        """
        if not config.VOTE_ENABLED:
            return ""
        req = flow.parse_vote_request(decision)
        if req is None:
            return ""
        if self._votes_held >= config.VOTE_MAX:
            log.info("表決請求被忽略：已達單場上限 %d（議題：%s）", config.VOTE_MAX, req["topic"])
            return ""
        self._votes_held += 1
        result = await self._hold_vote(
            ctx, req["topic"], req["options"], tag, token_usage_task_id=token_usage_task_id
        )
        return f"表決結果：{result['winner']}（議題：{req['topic']}）"

    async def _hold_vote(
        self,
        ctx: LaneContext,
        topic: str,
        options: list[str],
        tag: int | None,
        *,
        token_usage_task_id: int | None = None,
    ) -> dict:
        """舉行一場 3-AI 表決：PM ＋ 兩位「不同 provider」的一次性投票員多數決。

        副作用集中於此（查額度快照、建一次性投票員、發言、廣播 VOTE_RESULT）；決策是
        flow 純函式（pick_vote_providers 挑投票員、parse_ballot 解票、tally_votes 計票）。
        投票員以 _build_voter_role 液生（唯讀工具）＋（_vote_factory 或 providers.make_expert）
        建立，不進 roster、finally 必 stop；單一投票員失敗＝棄權，絕不拖垮流程。

        降級（degraded=True）：可用外部 provider 不足兩位、或無法安全建真投票員（呼叫端
        顯式注入 experts 的測試/離線假專家、離線模式、無 cwd——比照 _dispatch_task_expert
        的護欄，絕不把 stub 環境接上真 provider；注入 _vote_factory 的測試除外）→ 不建
        投票員，winner 取 PM 自己的票（棄權則第一選項），照樣廣播。計票無多數（平手且
        PM 棄權／全棄權）時同樣以「PM 的票，否則第一選項」兜底，保證回傳的 winner 非空、
        流程永遠可續行。回 ``{winner, ballots, tie, degraded}``。
        """
        await self._refresh_quota_snapshot()
        digest = provider_quota.digest(self._quota_snap) if self._quota_snap else {}
        pm_expert = ctx.experts.get("pm")
        pm_provider = (
            self._role_provider_map({"pm": pm_expert}).get("pm", "") if pm_expert else ""
        ) or "claude"
        voters = flow.pick_vote_providers(digest, exclude=pm_provider, n=2)
        can_build = self._vote_factory is not None or not (
            self._experts_injected or ctx.cwd is None or config.OFFLINE_MODE
        )
        degraded = len(voters) < 2 or not can_build
        opts_desc = "\n".join(f"- {o}" for o in options)
        ballot_prompt = (
            f"表決議題：{topic}\n可選選項：\n{opts_desc}\n\n"
            f"脈絡（本場需求）：{self._requirement}\n\n"
            "請獨立判斷並投票：簡短說明理由，最後一行輸出 `投票: <選項原文>`。"
        )
        ballots: list[dict] = []
        pm_text = await self._speak(
            ctx, "pm", ballot_prompt, tag, token_usage_task_id=token_usage_task_id
        )
        pm_choice = flow.parse_ballot(pm_text or "", options)
        ballots.append({"voter": "pm", "provider": pm_provider, "choice": pm_choice})
        if not degraded:
            for prov in voters:
                role = self._build_voter_role(prov)
                expert = None
                try:
                    if self._vote_factory is not None:
                        expert = self._vote_factory(role, ctx.cwd, prov)
                    else:
                        from .providers import make_expert

                        expert = make_expert(
                            role, f"{self.session_id}:vote:{prov}", ctx.cwd, provider=prov
                        )
                    async with self._llm_semaphore():
                        text = await expert.speak(
                            ballot_prompt,
                            self._tagged_broadcast(tag, token_usage_task_id=token_usage_task_id),
                        )
                    choice = flow.parse_ballot(text or "", options)
                except Exception:  # noqa: BLE001 — 單一投票員失敗＝棄權，表決不得拖垮流程
                    log.exception("表決投票員 %s 失敗，視為棄權（議題：%s）", prov, topic)
                    choice = ""
                finally:
                    if expert is not None:
                        try:
                            await expert.stop()  # best-effort：一次性投票員不進 roster，用完即回收
                        except Exception:  # noqa: BLE001
                            log.warning("表決投票員 %s stop 失敗", prov, exc_info=True)
                ballots.append({"voter": role.key, "provider": prov, "choice": choice})
        tally = flow.tally_votes(ballots)
        winner = tally["winner"] or pm_choice or options[0]  # 無多數→PM 票→第一選項兜底
        await self._tagged_broadcast(tag, token_usage_task_id=token_usage_task_id)(
            events.vote_result(
                self.session_id, topic, options, ballots, winner, tally["tie"], degraded=degraded
            )
        )
        self._record_vote_lesson(topic=topic, winner=winner, tie=tally["tie"], degraded=degraded)
        return {"winner": winner, "ballots": ballots, "tie": tally["tie"], "degraded": degraded}

    def _record_vote_lesson(self, *, topic: str, winner: str, tie: bool, degraded: bool) -> None:
        """把高品質表決結果落入跨場次教訓庫；失敗不得影響主流程。"""
        if tie or degraded:
            return
        topic = (topic or "").strip()
        winner = (winner or "").strip()
        if not topic or not winner:
            return
        try:
            lessons.add_many(
                [f"表決先例: {topic} → {winner}"],
                session_id=self.session_id,
                requirement=self._requirement,
                source="vote",
                exact_only=True,
            )
        except Exception:  # noqa: BLE001 — 教訓庫是旁路，表決事件流不得被阻斷
            log.warning("表決先例入庫失敗（議題：%s）", topic, exc_info=True)

    # --- 波次排程（並行支線）------------------------------------------
    def _min_lane_concurrency(self) -> int:
        """單一 lane 內最大同時 gather 數＝review 階段並行發言的 reviewer 數（資料驅動）。

        號誌下限須 ≥ 此值，否則單一 lane 的 review gather 會搶不到足夠額度而自我死鎖。
        依 workflow 的 review gate（過濾在場）動態計算，客製增減 reviewer 不必再手調。
        至少回 1（無 reviewer 時也不會把號誌夾成 0）。
        """
        return max(len(self._task_reviewers(self._get_experts())), 1)

    def _llm_semaphore(self) -> asyncio.Semaphore:
        """全域 LLM 並發節流號誌。下限夾到單一 lane 內最大 gather 數，避免該 lane review 自我死鎖。"""
        if self._llm_sem is None:
            self._llm_sem = asyncio.Semaphore(
                max(config.LLM_MAX_CONCURRENCY, self._min_lane_concurrency())
            )
        return self._llm_sem

    def _tagged_broadcast(
        self, task_id: int | None, *, token_usage_task_id: int | None = None
    ) -> Broadcast:
        """包裝 broadcast：lane 事件可分流；token_usage 可單獨補任務歸因。"""
        if task_id is None and token_usage_task_id is None:
            return self.broadcast

        async def _bc(ev: events.StudioEvent) -> None:
            if task_id is not None:
                ev.payload.setdefault("task_id", task_id)
            elif ev.type == events.EventType.TOKEN_USAGE:
                ev.payload.setdefault("task_id", token_usage_task_id)
            await self.broadcast(ev)

        return _bc

    async def _speak(
        self,
        ctx: LaneContext,
        role_key: str,
        prompt: str,
        task_id: int | None,
        *,
        token_usage_task_id: int | None = None,
    ) -> str:
        """經號誌節流 + 標籤化 broadcast 呼叫某 lane 的專家發言。"""
        async with self._llm_semaphore():
            try:
                return await ctx.experts[role_key].speak(
                    prompt,
                    self._tagged_broadcast(task_id, token_usage_task_id=token_usage_task_id),
                )
            except Exception as exc:  # noqa: BLE001 — provider-unavailable 要穿透，其餘維持既有路徑
                if getattr(exc, "provider", ""):
                    self._stop = True
                raise

    def _lane_tag(self, ctx: LaneContext, task: dict) -> int | None:
        """只有並行 lane 事件需要 task_id 分流；main lane 維持原主時間軸。"""
        if ctx.lane_id == "main":
            return None
        return task["id"]

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
        # 並行可觀測性：記錄波次/峰值支線數/合併衝突/各任務耗時，供 done 事件與 /api/metrics 量化。
        self._parallel_metrics = {
            "enabled": parallel,
            "waves": len(waves),
            "tasks": len(self._tasks),
            "lanes_max": 0,
            "merge_conflicts": 0,
            # 降級可觀測性：量化並行實際退回序列化的頻率，供 done / /api/metrics 診斷。
            "lane_exceptions": 0,  # lane 跑任務時拋例外（崩潰）→ 轉主幹序列化重跑的 lane 數
            "deferred": 0,  # worktree 開失敗、無法隔離 → 直接在主幹序列化跑的任務數
            "conflict_retries": 0,  # 合併衝突且 lane 內無法化解 → 在主幹序列化重跑的任務數
            "lane_resolved": 0,  # 合併衝突由 lane 內就地化解、保留 lane commit 的次數
            "_task_durations": [],
        }
        t0 = time.monotonic()
        all_ok = True
        for wave in waves:
            # 中止或過軟性時間預算 → 不再開新波次；剩餘波次任務留 todo（→ unmet → known-limit）。
            if self._stop or self._should_wind_down():
                if self._deadline_hit:
                    all_ok = False  # 時間截斷未跑完所有波次 → 不謊報全完成
                break
            # 並行模式：波次邊界先 drain 一次當本波基準（回顯已於收到時即時 broadcast，此處不重複）。
            # 波次內各 lane 另於每個任務再 drain 新插話累加（見 _lane_human_prefix），故波次跑到一半
            # 送的插話也進得來，不必枯等下一波。
            if parallel:
                self._pending_human = self._drain_human()
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
            # 峰值並行度＝任一波次內實際同時跑的 lane 數。
            self._parallel_metrics["lanes_max"] = max(
                self._parallel_metrics["lanes_max"], len(opened)
            )
            # worktree 開失敗無法隔離 → 計入 deferred 指標（稍後在主幹序列化重跑）。
            self._parallel_metrics["deferred"] += len(deferred)
            results = await asyncio.gather(
                *(self._run_lane(ctx, tasks, plan_ctx) for ctx, tasks in opened),
                return_exceptions=True,
            )
            all_ok = await self._integrate_wave(opened, results, deferred, plan_ctx) and all_ok
        self._finalize_parallel_metrics(time.monotonic() - t0)
        return all_ok

    def _finalize_parallel_metrics(self, wall_clock_s: float) -> None:
        """收尾並行指標：以各任務耗時總和估算「若循序」的時間，算出加速比。"""
        m = self._parallel_metrics
        durations = m.pop("_task_durations", [])
        m["wall_clock_s"] = round(wall_clock_s, 2)
        m["serial_estimate_s"] = round(sum(durations), 2)
        m["speedup"] = round(m["serial_estimate_s"] / wall_clock_s, 2) if wall_clock_s > 0 else 1.0

    def _lane_budget(self) -> int:
        """單一波次可同時並行的支線上限（依 LLM 並發預算自適應）。

        每條 lane 的 review 階段會同時佔用 `_min_lane_concurrency()` 個號誌額度（qa/senior/
        security 並行）。能真正同時推進的 lane 數 ≈ 全域 LLM 並發 ÷ 每 lane 佔用，向下取整。
        超過此數的 lane 只會卡在號誌前枯等，徒增 worktree 開／合併開銷 → 以此夾住上限。至少 1。
        """
        return max(1, config.LLM_MAX_CONCURRENCY // self._min_lane_concurrency())

    def _plan_lanes(self, wave: list[dict]) -> list[list[dict]]:
        """把一波任務切成多條支線（round-robin）。關閉並行/無 cwd 時整波一條。

        支線數隨波次大小自適應，但同時受使用者上限 `PARALLEL_LANES` 與 LLM 並發預算
        （`_lane_budget`）雙重約束——不開出多到只能在 LLM 號誌前排隊的 lane。
        預設設定（PARALLEL_LANES=3、LLM_MAX_CONCURRENCY=9、3 位 reviewer → 預算=3）下，
        上限維持 3，行為與調整前一致。
        """
        if not (config.PARALLEL_TASKS_ENABLED and self.cwd):
            return [wave]
        n = max(1, min(config.PARALLEL_LANES, self._lane_budget(), len(wave)))
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
        # 子題 id 每個 session/波次都從 1 重編，光用 "task-<id>" 會跨 session 撞名
        # （上一輪 timeout/被 kill 沒走到 teardown，殘留的 task-1 分支毒下一輪 → exit 255）。
        # 加 session_id 前綴使分支全域唯一；git_worktree_add 另會 prune+清同名殘留作雙保險。
        branch = f"lane-{self.session_id}-" + "-".join(str(t["id"]) for t in lane_tasks)
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
        await self._lane_git_snapshot("open", branch)
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
        """收掉一條並行 lane 的專家連線與 worktree（best-effort，整段有界收斂）。

        進入前先 broadcast phase_change("清理") 作為 history/watchdog 可見錨點，讓「卡在 teardown」
        與「卡在 task」可被區分。

        expert stop 用 `asyncio.wait(timeout=…)` 的 **abandon-pending** 收斂，而非 gather+timeout：
        #261 的真正根因是 stop()→disconnect() 在 anyio 下**吞取消**永不返回，`asyncio.timeout`/`gather`
        用同一套協作式取消，對它完全穿不透（決定性 probe 實證：連外層 wait_for 都會被一起拖死）。
        `asyncio.wait` 逾時回傳 (done, pending) 但**不取消、不 await pending**——卡住的 stop() 協程於
        背景洩漏（其 SDK 子行程已由 stop() 內層 _best_effort_kill 兜底 SIGKILL），主流程於上界內續行。
        並行啟動所有 stop()，最壞時間為單一上界而非 Σ（不隨 lane×expert 數線性放大）。

        兩處呼叫端（_integrate_wave 崩潰 lane 收尾與正常合併後收尾）皆為裸 await、無外層 except，
        故本函式所有例外/逾時一律就地吸收，不外拋（逸出會跳過後續 lane 合併/重跑/flush，破防 best-effort）。
        """
        # 錨點放在收斂邏輯外：即使後續全數卡住，監控仍看得到「已進入清理」。
        await self.broadcast(
            events.phase_change(self.session_id, "清理", f"收掉 lane {ctx.lane_id}")
        )
        # 整段共享同一個 deadline，stop 與 git 收尾合計不超過一個上界（非各段各等一次）。
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _TEARDOWN_LANE_TIMEOUT
        experts = list(ctx.experts.values()) + list((ctx.critics or {}).values())
        if experts:
            # ensure_future 顯式建 task；abandon-pending 是對「不可取消 hang」唯一有界的收斂法。
            tasks = [asyncio.ensure_future(ex.stop()) for ex in experts]
            remaining = max(0.0, deadline - loop.time())
            _done, pending = await asyncio.wait(tasks, timeout=remaining)
            if pending:
                # 放手不 await、也不 cancel（吞取消者 cancel 無效且會反噬拖死本協程）；純記錄洩漏。
                log.warning(
                    "lane %s teardown：%d/%d 個 expert stop() 逾 %.0fs 未返回，放手繼續（背景洩漏）",
                    ctx.lane_id,
                    len(pending),
                    len(tasks),
                    _TEARDOWN_LANE_TIMEOUT,
                )
        # git 收尾：worktree_remove 底層 run_command_exec timeout=30/20；snapshot 兩個 probe 皆 timeout=20；這些都是可取消 subprocess（communicate + killpg），不重複包每個 git await。
        # 天然有界；外層以 timeout_at(deadline) 共享剩餘預算——stop 吃光則 git 收尾立即放棄，符合 best-effort。
        try:
            async with asyncio.timeout_at(deadline):
                if self.cwd and ctx.cwd and ctx.branch:
                    await runner.git_worktree_remove(self.cwd, ctx.cwd, ctx.branch)
                await self._lane_git_snapshot("teardown", ctx.branch)
        except TimeoutError:
            log.warning(
                "lane %s teardown git 收尾超出整段上界 %.0fs，放棄剩餘 git 收尾繼續主流程",
                ctx.lane_id,
                _TEARDOWN_LANE_TIMEOUT,
            )

    async def _lane_git_snapshot(self, where: str, branch: str | None = None) -> None:
        """診斷用：DEBUG 等級時記錄主工作樹的 git 狀態，定位「lane 成果漏進主工作樹」根因。

        並行 lane 隔離理應讓主工作樹（self.cwd）只透過 _merge_lane 取得成果；實測卻見主工作樹
        出現未追蹤的 lane 檔、lane 分支 merge 變 no-op（master 未前進卻仍廣播「合併支線」）。
        本快照在 lane 開/合/收邊界記錄主工作樹 HEAD、porcelain 狀態、以及該 lane 分支是否已
        reachable，讓下一次乾淨重現把狀態轉變如實錄下，而非事後從已污染的 workspace 臆測。
        以 log.debug 自動 gate（INFO 預設不觸發），且僅在啟用 DEBUG 時才跑 git——零行為改變、
        平時零成本。設 `logging.getLogger("ti.orchestrator").setLevel(logging.DEBUG)` 即開。
        """
        if not (self.cwd and log.isEnabledFor(logging.DEBUG)):
            return
        try:
            head = await runner.git_head_short(self.cwd)
            st = await runner.run_command_exec(
                self.cwd, ["git", "status", "--porcelain"], sandbox=False, timeout=20
            )
            reachable = None
            if branch:
                chk = await runner.run_command_exec(
                    self.cwd,
                    ["git", "merge-base", "--is-ancestor", branch, "HEAD"],
                    sandbox=False,
                    timeout=20,
                )
                reachable = chk.ok  # True＝該分支已併入主幹（merge 真的落地）
            log.debug(
                "lane-snapshot[%s] branch=%s main_HEAD=%s branch_reachable=%s status=%r",
                where,
                branch,
                head,
                reachable,
                (st.output or "")[:600],
            )
        except Exception:  # noqa: BLE001 — 診斷絕不可拖垮主流程
            log.debug("lane-snapshot[%s] branch=%s 失敗（已忽略）", where, branch, exc_info=True)

    async def _run_lane(
        self, ctx: LaneContext, lane_tasks: list[dict], plan_ctx: str
    ) -> LaneResult:
        """在指定 lane 依序跑完配給的任務（lane 之間由 _run_waves 以 gather 並行）。"""
        lane_ok = True
        for task in lane_tasks:
            # 中止或過軟性時間預算 → 不再派發本 lane 後續任務；未動任務留 todo（→ unmet → known-limit）。
            # lane_ok 置 False 使 all_ok 反映「未全數完成」，據此走帶已知限制出貨而非謊報全完成。
            if self._stop or self._should_wind_down():
                lane_ok = False
                break
            lane_ok = await self._run_task_in_lane(ctx, task, plan_ctx) and lane_ok
        return LaneResult(ctx=ctx, tasks=lane_tasks, ok=lane_ok)

    async def _run_task_in_lane(self, ctx: LaneContext, task: dict, plan_ctx: str) -> bool:
        """在指定 lane 跑單一任務（實作→驗證→審查→huddle），更新看板與 lane 知識緩衝。"""
        # 並行 lane 事件帶 task_id 供前端分流；主 lane 僅 token_usage 補 task_id 做歸因。
        bc = self._tagged_broadcast(self._lane_tag(ctx, task), token_usage_task_id=task["id"])
        await bc(
            events.phase_change(self.session_id, "實作", f"任務 #{task['id']}：{task['title']}")
        )
        await self._set_task_status(task, "doing", bc)
        t0 = time.monotonic()
        task_ok = await self._work_task(ctx, task, plan_ctx)
        # 卡關升級：跑滿輪數仍未通過 → 召集 huddle 討論替代方案 + 給 1 輪重試。
        # 過軟性時間預算則不再開 huddle（又一整輪討論+重試）——已超時就收尾出貨，未過記 known-limit。
        if (
            not task_ok
            and config.HUDDLE_ENABLED
            and not self._stop
            and not self._should_wind_down()
        ):
            task_ok = await self._huddle_and_retry(ctx, task, plan_ctx, bc)
        # 累計本任務耗時（供「若循序」估算 → 加速比）。
        self._parallel_metrics.setdefault("_task_durations", []).append(time.monotonic() - t0)
        await self._set_task_status(task, "done" if task_ok else "review", bc)
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

    async def _integrate_wave(
        self,
        opened: list[tuple[LaneContext, list[dict]]],
        results: list,
        deferred: list[dict],
        plan_ctx: str,
    ) -> bool:
        """波次收尾（全序列化、無競態）：合併各 lane 回主分支 → flush 知識 → 清 worktree。

        results 與 opened 位置對應（asyncio.gather 保序）。某 lane 跑任務時拋例外（崩潰）→ 丟棄
        該 lane 的 worktree 與筆記，把其任務併入 deferred 於主幹序列化重跑（與合併衝突 fallback
        對稱，不讓崩潰 lane 的任務靜默卡在 doing/review）。

        【#3 過渡段非 LLM await 稽核】全段非 LLM await timeout 覆蓋如下：
        - `broadcast`：委派 ws.py send_json，無本地 wait_for；為無界網路 await（stalled client
          可阻塞）。不加外層信封：事件已先 `history.record_event` 落檔（前端可 attach 補放），
          broadcast 阻塞不遺失資料，且不影響 lane 合併邏輯。已如實記入 transition_await_inventory.md。
        - `_teardown_lane`：整段共享單一 deadline（_TEARDOWN_LANE_TIMEOUT），expert stop 以
          abandon-pending 收斂（對 anyio 吞取消唯一有效做法），git 收尾委派 runner.* 各帶 timeout。
        - `_merge_lane`：所有 git 操作委派 runner.*，底層走 run_command_exec → _finalize_proc →
          asyncio.wait_for(communicate(), timeout) + killpg——真 subprocess，asyncio 協作
          取消有效，逐項 timeout 足夠，**無需外層信封**（對比 teardown：stop 的 anyio 吞取消
          才是外層信封的動機）。詳見 _merge_lane 的稽核說明。
        - `_flush_lane_notes`：同步函式，無 await，不需 timeout。
        - `_run_task_in_lane`：LLM await，由 TURN_IDLE_TIMEOUT / TURN_HARD_TIMEOUT 覆蓋，
          超出本段稽核範圍。
        """
        all_ok = True
        lane_results: list[LaneResult] = []
        crashed: list[dict] = []
        for (ctx, tasks), r in zip(opened, results, strict=True):
            if isinstance(r, BaseException):
                self._parallel_metrics["lane_exceptions"] = (
                    self._parallel_metrics.get("lane_exceptions", 0) + 1
                )
                await self.broadcast(
                    events.error(
                        self.session_id,
                        f"lane 例外：{type(r).__name__}: {r}，改於主幹序列化重跑",
                    )
                )
                # 崩潰 lane：丟棄其 worktree／筆記（成果未合併、不可信），任務改主幹重跑。
                # 不在此直接判失敗——最終是否通過交由主幹重跑決定（與合併衝突 fallback 對稱）。
                if ctx is not self._main_ctx:
                    ctx.notes_buffer.clear()
                    await self._teardown_lane(ctx)
                crashed.extend(tasks)
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
        # 無法隔離（worktree 開失敗）+ 崩潰 lane 的任務：序列化重跑在主 lane。
        for task in deferred + crashed:
            if self._stop:
                break
            all_ok = await self._run_task_in_lane(self._main_ctx, task, plan_ctx) and all_ok
        # 主 lane 緩衝（含序列化重跑/降級/循序模式）一併 flush。
        self._flush_lane_notes(self._main_ctx)
        return all_ok

    async def _merge_lane(self, lr: LaneResult, plan_ctx: str) -> bool:
        """把一條並行 lane 的分支合併回主分支。

        衝突時先嘗試「lane 內解衝突」：把最新主幹 merge 進 lane worktree，讓該 lane 的工程師
        就地解掉衝突標記後 commit，再 fast-forward 合回主幹——成功則保留 lane 已完成的所有
        commit（省去整段重跑）。解不掉才退回既有的「於最新主幹序列化重跑」fallback。

        【#3 非 LLM await timeout 來源】本函式所有 git subprocess 呼叫均委派 runner.*，底層
        run_command_exec → _finalize_proc → asyncio.wait_for(communicate(), timeout) +
        killpg；asyncio 協作取消對 subprocess 有效（對比 SDK anyio 吞取消），逐項 timeout 足夠：
        - _lane_git_snapshot：內部每個 run_command_exec timeout=20，外層 try/except BLE001。
        - git_merge_worktree：timeout=60（含 merge commit 寫入）。
        - git_head_short：timeout=20。
        - git_merge_abort：timeout=20。
        - broadcast：無本地 wait_for，為無界網路 await（詳見 _integrate_wave 稽核說明）。
        - _resolve_conflict_in_lane / _serialize_lane_rerun：LLM await，TURN timeout 覆蓋。
        """
        await self._lane_git_snapshot("pre-merge", lr.ctx.branch)
        res = await runner.git_merge_worktree(self.cwd, lr.ctx.branch)
        log.debug(
            "merge-result branch=%s ok=%s conflict=%s blocked=%s out=%r",
            lr.ctx.branch,
            res.ok,
            res.conflict,
            res.blocked,
            (res.output or "")[:300],
        )
        if res.ok:
            h = await runner.git_head_short(self.cwd)
            if h:
                self._last_commit = h  # 下一波 worktree 以此為 base，必含本波已合併變更。
                await self.broadcast(
                    events.git_commit(self.session_id, f"合併支線 {lr.ctx.branch}", h)
                )
            await self._lane_git_snapshot("post-merge-ok", lr.ctx.branch)
            return lr.ok
        if res.conflict:
            self._parallel_metrics["merge_conflicts"] = (
                self._parallel_metrics.get("merge_conflicts", 0) + 1
            )
            await runner.git_merge_abort(self.cwd)
            # 先試 lane 內解衝突（保留 lane 工作）；成功即合回主幹、不必序列化重跑。
            if await self._resolve_conflict_in_lane(lr, plan_ctx):
                self._parallel_metrics["lane_resolved"] = (
                    self._parallel_metrics.get("lane_resolved", 0) + 1
                )
                return lr.ok
            return await self._serialize_lane_rerun(
                lr,
                plan_ctx,
                reason=f"支線 {lr.ctx.branch} 衝突且 lane 內無法化解，於最新主幹序列化重跑",
            )
        if res.blocked:
            # 合併還沒開始就被工作樹擋下（主工作樹有未追蹤檔會被覆寫，或有未提交本地修改）：
            # 無 MERGE_HEAD 可 abort，且這些檔案就在主工作樹裡。直接序列化重跑——重跑在主工
            # 作樹就地完成、git_commit 的 `add -A` 會把既有檔案一併收進來，不會像過去那樣把
            # 整條 lane 成果當未知硬失敗丟掉、讓 session 帶著殘缺產出繼續。
            self._parallel_metrics["merge_blocked"] = (
                self._parallel_metrics.get("merge_blocked", 0) + 1
            )
            return await self._serialize_lane_rerun(
                lr,
                plan_ctx,
                reason=(
                    f"支線 {lr.ctx.branch} 因主工作樹未追蹤檔／未提交修改無法合併，"
                    "於最新主幹序列化重跑"
                ),
            )
        await self.broadcast(
            events.error(self.session_id, f"支線 {lr.ctx.branch} 合併失敗：{res.output[:200]}")
        )
        return False

    async def _serialize_lane_rerun(self, lr: LaneResult, plan_ctx: str, *, reason: str) -> bool:
        """lane 無法乾淨合回主幹時的共用 fallback：丟棄 lane 筆記，於最新主幹（主工作樹）
        逐一序列化重跑該 lane 的任務。內容衝突解不掉、與工作樹受阻（未追蹤檔／未提交修改）
        都走這條，確保並行成果一律有去處、不被靜默丟棄。"""
        lr.ctx.notes_buffer.clear()  # 改以序列化重跑為準，丟棄並行 lane 的中途筆記。
        await self.broadcast(events.phase_change(self.session_id, "合併衝突", reason))
        ok = True
        for task in lr.tasks:
            if self._stop:
                ok = False
                break
            self._parallel_metrics["conflict_retries"] = (
                self._parallel_metrics.get("conflict_retries", 0) + 1
            )
            ok = await self._run_task_in_lane(self._main_ctx, task, plan_ctx) and ok
        return ok

    async def _resolve_conflict_in_lane(self, lr: LaneResult, plan_ctx: str) -> bool:
        """在 lane 的 worktree 內就地化解與主幹的合併衝突，成功則 fast-forward 合回主幹。

        流程：把最新主幹 HEAD merge 進 lane 分支（留下衝突標記）→ 工程師就地解標記 → 確認無
        殘留標記 → 完成 merge commit → 合回主幹。任一步失敗一律回 False（呼叫端走序列化重跑
        fallback），且把主 repo 還原乾淨，不留半完成狀態。
        """
        if not (self.cwd and lr.ctx.cwd and lr.ctx.branch and self._last_commit):
            return False
        if self._stop or "engineer" not in lr.ctx.experts:
            return False
        tag = lr.tasks[0]["id"] if lr.tasks else None
        # 1) 把最新主幹 merge 進 lane 分支（保留衝突標記，不自動 abort）。
        m = await runner.git_merge_ref_into(lr.ctx.cwd, self._last_commit)
        if m.ok:
            # 罕見：與主幹其實可自動合 → 直接完成 merge commit 後合回。
            if not await runner.git_commit(lr.ctx.cwd, f"併入主幹 {self._last_commit}"):
                return False
            return await self._merge_resolved_lane_back(lr)
        if not m.conflict:
            return False  # 非衝突的其他失敗 → 走 fallback
        await self.broadcast(
            events.phase_change(
                self.session_id,
                "合併衝突",
                f"支線 {lr.ctx.branch} 與主幹衝突，由該支線工程師就地化解",
            )
        )
        # 2) 工程師就地解衝突標記（其 cwd 即此 worktree，可直接編輯/執行）。
        await self._speak(
            lr.ctx,
            "engineer",
            "你的分支與主幹合併時發生衝突。請開啟下列衝突檔案、逐處化解 `<<<<<<<` / "
            "`=======` / `>>>>>>>` 標記（保留雙方意圖、勿刪他人變更），確保不留任何衝突標記、"
            f"且程式仍可執行。整體計畫供參考：\n{plan_ctx}\n\ngit 合併輸出：\n{m.output[:1500]}",
            tag,
        )
        if self._stop:
            await runner.git_merge_abort(lr.ctx.cwd)
            return False
        # 3) 確認衝突標記已清空（殘留即視為未解，走 fallback）。
        if await runner.git_conflict_markers_present(lr.ctx.cwd):
            await runner.git_merge_abort(lr.ctx.cwd)
            return False
        # 4) 完成 merge commit（add -A 收下解好的檔案）。
        if not await runner.git_commit(lr.ctx.cwd, f"化解與主幹 {self._last_commit} 的合併衝突"):
            await runner.git_merge_abort(lr.ctx.cwd)
            return False
        return await self._merge_resolved_lane_back(lr)

    async def _merge_resolved_lane_back(self, lr: LaneResult) -> bool:
        """lane 已併入主幹並解完衝突後，把它合回主分支（此時應可乾淨快轉）。"""
        res = await runner.git_merge_worktree(self.cwd, lr.ctx.branch)
        if not res.ok:
            await runner.git_merge_abort(self.cwd)  # 仍不乾淨 → 還原主 repo，走 fallback。
            return False
        h = await runner.git_head_short(self.cwd)
        if h:
            self._last_commit = h
            await self.broadcast(
                events.git_commit(self.session_id, f"合併支線 {lr.ctx.branch}（已化解衝突）", h)
            )
        return True

    async def _work_task(
        self,
        ctx: LaneContext,
        task: dict,
        pm_plan: str,
        *,
        max_rounds: int | None = None,
        seed_feedback: str = "",
    ) -> bool:
        """單一任務入口：先做額度感知 per-task 派工（暫換實作者的 provider/model），再跑實作迴圈。

        換綁只影響本任務：finally 一定還原原專家並 best-effort stop 臨時專家（含中途 return／
        例外／stop 路徑）。choose 回空 provider（額度全掛/查詢失敗）、與現綁定相同、或處於
        測試 stub／離線護欄時不換綁＝與既有行為完全一致。實作迴圈本體在 _work_task_rounds。
        """
        # 實作者：task_pipeline 的 implement.assignee（預設 engineer）；不在場則退回 engineer。
        impl_role = self._task_implementer()
        if impl_role not in ctx.experts:
            impl_role = "engineer"
        t0 = time.monotonic()
        restore = await self._dispatch_task_expert(ctx, task, impl_role)
        try:
            return await self._work_task_rounds(
                ctx, task, pm_plan, impl_role, max_rounds=max_rounds, seed_feedback=seed_feedback
            )
        finally:
            # 考核客觀指標：於還原換綁「前」收集（此時 _dispatch_bindings 仍指向本任務
            # 實際綁定），提供 _wrap_up 與 PM 主觀評分合併入考核庫。永不 raise。
            self._collect_task_perf(ctx, task, impl_role, time.monotonic() - t0)
            perf = self._task_perf.get(task["id"])
            try:
                if perf:
                    try:
                        await self.broadcast(
                            events.task_result(
                                self.session_id,
                                task["id"],
                                role=perf.get("role") or impl_role,
                                provider=perf.get("provider") or "",
                                model=perf.get("model"),
                                duration_s=perf.get("duration_s"),
                                qa_rounds=perf.get("qa_rounds"),
                                input_tokens=perf.get("input_tokens"),
                                output_tokens=perf.get("output_tokens"),
                                total_tokens=perf.get("total_tokens"),
                                cost_usd=perf.get("cost_usd"),
                                cost_source=perf.get("cost_source"),
                            )
                        )
                    except Exception:  # noqa: BLE001
                        log.warning("廣播 task_result 事件失敗（略過）", exc_info=True)
            finally:
                await restore()

    async def _work_task_rounds(
        self,
        ctx: LaneContext,
        task: dict,
        pm_plan: str,
        impl_role: str,
        *,
        max_rounds: int | None = None,
        seed_feedback: str = "",
    ) -> bool:
        """單一任務的 實作→自測→驗證→審查→改進 迴圈，回傳是否通過。

        所有工作（cwd / 專家 / commit / NOTES）都綁定在傳入的 lane context 上，循序模式
        傳 main_ctx＝今日行為，並行模式傳各 lane 的隔離 context。
        impl_role：實作者 role key（由 _work_task 解析並可能已被 per-task 派工暫時換綁）。
        max_rounds：限制本次迴圈輪數（huddle 後重試只給 1 輪）；None 用 config 預設。
        seed_feedback：預先注入的回饋（huddle 結論），非空時第一輪即走「改進」路徑。
        """
        # reviewer 集合（資料驅動，過濾在場）：預設 qa/senior＋security 在場才審，重現今日。
        reviewers = self._task_reviewers(ctx.experts)
        tag = self._lane_tag(ctx, task)
        bc = self._tagged_broadcast(tag, token_usage_task_id=task["id"])
        feedback = seed_feedback
        # 輪數：huddle 重試顯式傳 max_rounds 優先；否則 review stage 的 max_rounds 覆寫，再否則 config。
        rounds = (
            max_rounds
            if max_rounds is not None
            else (self._task_max_rounds() or config.TASK_MAX_ROUNDS)
        )
        critic_rejects = 0  # 客觀全綠下 critic 退回次數，達 CRITIC_MAX_REJECTS 即收斂放行
        help_used = 0  # 中途求助 PM 次數（「每任務」上限 TASK_HELP_MAX，防多輪 × 多次燒 token）
        impl_history: list[str] = []  # 各輪工程師發言，供停滯偵測
        prev_commit = ctx.last_commit
        for rnd in range(1, rounds + 1):
            # 中止或過軟性時間預算 → 結束本任務迴圈（未過＝known-limit）。此檢查必須在任務內「每輪」
            # 都做：時間多半耗在單任務的多輪實作/審查/huddle 裡，只在 _run_lane/_run_waves 派發邊界
            # 檢查會整輪漏掉、撐到硬 timeout（見 #217 後驗證:core #27 卡在單任務迴圈、收斂事件沒觸發）。
            if self._stop or self._should_wind_down():
                return False
            human = await self._lane_human_prefix(ctx)

            # --- 實作 ---
            impl_prompt = self._build_impl_prompt(
                ctx,
                task,
                pm_plan,
                rnd=rnd,
                feedback=feedback,
                seed_feedback=seed_feedback,
                help_used=help_used,
                human=human,
            )
            impl_text = await self._speak(
                ctx, impl_role, impl_prompt, tag, token_usage_task_id=task["id"]
            )

            # --- 中途求助 PM：工程師輸出 `求助: <問題>` 時就地問 PM 拿指示、續作後覆蓋 impl_text ---
            impl_text, help_used = await self._mid_task_help(
                ctx,
                task,
                pm_plan,
                impl_role,
                impl_text,
                help_used=help_used,
                human=human,
                tag=tag,
                bc=bc,
            )

            # --- 交付前自測（確定性 smoke-run）---
            smoke = await self._self_test(ctx, impl_text, bc)
            # 自測指令是否為工程師「本輪自己宣告」：宣告者代表工程師聲稱此指令能展示本任務，
            # 實敗才適用硬性閘門/就地精修；fallback 到 PM 的整體執行指令時只回報不硬退——
            # 多任務場景下整體指令在前期任務本來就跑不起來，硬退回會誤殺（strict 模式除外）。
            own_cmd = runner.parse_run_command(impl_text) is not None
            # --- (D) 單輪內自我精修：自測「實際執行」未通過時，讓同一工程師就地依執行紀錄再修 ---
            impl_text, smoke = await self._self_refine(
                ctx,
                task,
                impl_role,
                impl_text,
                smoke,
                own_cmd=own_cmd,
                human=human,
                tag=tag,
                bc=bc,
            )
            commit_violations = (
                await self._commit(
                    ctx,
                    f"任務#{task['id']} 第{rnd}輪：{task['title']}",
                    bc,
                    forbidden_paths=task.get("forbidden_paths") or None,
                )
                or []
            )

            # --- 停滯守門：連續多輪只重述且無檔案變動 → 提早收斂，不再燒後續 token ---
            impl_history.append(impl_text)
            committed_change = ctx.last_commit != prev_commit
            prev_commit = ctx.last_commit
            if self._stalled(ctx, impl_history, committed_change):
                await bc(
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

            if commit_violations:
                feedback = self._forbidden_paths_feedback(task, commit_violations)
                await self._store_reflection(ctx, task, rnd, impl_text, feedback, bc)
                continue

            # --- 過軟性時間預算：本輪實作已 commit，但時間已過軟 deadline → 不再開昂貴的
            # 三審 fan-out（QA/senior/security 各一次 LLM turn，是單輪最大且最易把整輪拖過硬
            # timeout 被砍的開銷）。提早收尾：已 commit 成果交由 session 收尾依客觀證據決定出貨，
            # 本任務未過審記未達（unmet → known-limit）。補 #217 盲點——每輪「頂端」檢查擋不住
            # 「單輪本身超長」的稽核型任務：reviewer fan-out 在軟 deadline 後才開始，就會一路撐到
            # 硬 timeout、整場記 timeout failed（見 autopilot #83：3060s 過軟 deadline 後仍跑滿到
            # 3600s 被硬砍）。_should_wind_down() 已置 _deadline_hit，收尾階段據此走 Demo/出貨而非硬丟。
            if self._should_wind_down():
                await bc(
                    events.phase_change(
                        self.session_id,
                        "時間預算收尾",
                        f"任務 #{task['id']} 已過軟性時間預算，跳過剩餘審查、提早收尾出貨",
                    )
                )
                self._note(
                    ctx,
                    f"## 時間預算收尾 任務 #{task['id']}：{task['title']}"
                    "（過軟性時間預算，本輪實作已 commit 但未過審，記未達）",
                )
                return False

            # --- 驗證 + 審查 + 資安：三者都評同一份已 commit 的實作、互相獨立 → 並行省時 ---
            all_review_ok, review_section = await self._run_reviews(
                ctx, task, pm_plan, reviewers, rnd=rnd, tag=tag, bc=bc
            )

            # --- 放行判定：客觀閘門 → critic 異議 → dynamic consult ---
            passed, feedback, critic_rejects = await self._release_verdict(
                ctx,
                task,
                pm_plan,
                rnd=rnd,
                smoke=smoke,
                own_cmd=own_cmd,
                all_review_ok=all_review_ok,
                review_section=review_section,
                critic_rejects=critic_rejects,
                tag=tag,
                bc=bc,
            )
            if passed:
                return True
            # 所有退回路徑統一收尾：把本輪教訓蒸餾成反思，帶 feedback 進下一輪。
            await self._store_reflection(ctx, task, rnd, impl_text, feedback, bc)
        return False

    async def _release_verdict(
        self,
        ctx: LaneContext,
        task: dict,
        pm_plan: str,
        *,
        rnd: int,
        smoke: runner.RunOutput | None,
        own_cmd: bool,
        all_review_ok: bool,
        review_section: str,
        critic_rejects: int,
        tag: int | None,
        bc: Broadcast,
    ) -> tuple[bool, str, int]:
        """本輪放行判定：客觀閘門（硬性否決）→ critic 異議 → dynamic consult。

        回傳 (是否放行, 退回 feedback, 更新後 critic_rejects)。放行時 feedback 為空字串
        （呼叫端不使用）；退回時呼叫端統一以 feedback 蒸餾反思並進下一輪——原各退回
        路徑的最後一步都是 _store_reflection，故可安全上提，順序不變。
        控制流以旗標 dispatch：return True/continue 的決策權留在外層迴圈。
        """
        # --- (B) 客觀閘門（硬性否決）：交付前自測「實際執行」未通過 → 本輪強制退回，
        # QA/高工的文字裁決推翻不了真實 exit code（守住反 reward-hacking）。只在「工程師
        # 本輪自己宣告的自測指令」真的有跑且失敗時否決——fallback 到整體執行指令的失敗
        # 只回報、不硬退（前期任務整體指令本來就跑不起來）；strict 模式維持全面嚴格：
        # fallback 失敗與「未宣告自測指令」皆視為未通過。評審照常並行跑（評同一 commit、
        # 文字仍是修正素材），附在閘門結論之後。---
        gate_veto = (
            config.objective_gate_enabled()
            and ctx.cwd is not None
            and (
                (smoke is not None and not smoke.ok and (own_cmd or config.objective_gate_strict()))
                or (smoke is None and config.objective_gate_strict())
            )
        )
        if gate_veto:
            if smoke is not None:
                gate_note = (
                    f"【客觀閘門】交付前自測「{smoke.command}」實際執行未通過"
                    f"（exit={smoke.exit_code}{'，逾時' if smoke.timed_out else ''}），本輪強制退回。\n"
                    f"執行紀錄：\n{smoke.output}"
                )
            else:
                gate_note = (
                    "【客觀閘門】嚴格模式：未宣告任何可執行的自測指令，無從客觀驗證，本輪強制退回。"
                )
            review_note = f"\n\n{review_section}" if review_section else ""
            await bc(
                events.phase_change(
                    self.session_id,
                    "客觀閘門",
                    f"任務 #{task['id']} 交付前自測實際執行未通過，第 {rnd} 輪強制退回",
                )
            )
            self._note(ctx, f"## 客觀閘門退回 任務 #{task['id']}：{task['title']}")
            return False, gate_note + review_note, critic_rejects

        if all_review_ok:
            # 放行前異議關卡：用 pm 視角（避開剛審查表態的 senior）獨立挑錯。
            # workflow 省略 gate stage 時整關跳過（視為放行）；預設含 gate stage→沿用今日，
            # 實際是否發 critic 仍由 _critic_gate 內的 config.CRITIC_ENABLED 決定。
            subject = f"任務 #{task['id']}：{task['title']}"
            if self._task_critic_enabled():
                critic_ok, critic_text = await self._critic_gate(ctx, "pm", subject, pm_plan, bc)
            else:
                critic_ok, critic_text = True, ""
            if critic_ok:
                # task 級 dynamic consult：PM 動態追加把關（task_pipeline 無 dynamic stage 時
                # 直接放行＝零行為變更）。被追加專家提出阻擋異議 → 退回再修。
                blocked, dyn_feedback = await self._task_dynamic_consult(
                    ctx, task, review_section, tag, bc
                )
                if not blocked:
                    return True, "", critic_rejects
                return False, dyn_feedback, critic_rejects
            # 收斂預算：此處已過 qa/senior/security/客觀閘門（gate_veto 已先退回），即「客觀全綠」，
            # critic 僅剩語意異議。達 CRITIC_MAX_REJECTS 次仍提不出可重現紅點 → 客觀證據優先，以
            # 已知限制放行，並把殘留疑慮記成後續任務（不靜默丟），避免無限退回燒滿輪數後整場判失敗。
            critic_rejects += 1
            # 質量事件留痕（僅落檔不推播）：信任指標的 critic 退回計數（events.jsonl）。
            notify.record("critic_reject", task_id=task.get("id"), rejects=critic_rejects)
            if config.CRITIC_MAX_REJECTS > 0 and critic_rejects >= config.CRITIC_MAX_REJECTS:
                self._followups.append(
                    f"覆查 critic 對「{task['title']}」的殘留疑慮"
                    f"（客觀閘門全綠、{critic_rejects} 次退回均無可重現紅點）：{critic_text[:160]}"
                )
                self._note(
                    ctx,
                    f"## critic 收斂放行 任務 #{task['id']}：{task['title']}"
                    f"（客觀全綠、critic 連退 {critic_rejects} 次無可重現紅點，以已知限制放行）\n{critic_text}",
                )
                await bc(
                    events.phase_change(
                        self.session_id,
                        "critic 收斂",
                        f"任務 #{task['id']} 客觀閘門全綠、critic 退回達上限"
                        f"（{critic_rejects} 次），以已知限制放行",
                    )
                )
                return True, "", critic_rejects
            # 異議成立 → 退回再修，把反對理由帶進下一輪並記入知識庫。
            feedback = f"【異議檢查（critic）退回理由】\n{critic_text}"
            self._note(ctx, f"## 異議退回 任務 #{task['id']}：{task['title']}\n{critic_text}")
            await bc(
                events.phase_change(
                    self.session_id,
                    "異議退回",
                    f"任務 #{task['id']} 表面通過但 critic 提出實質反對，退回修正",
                )
            )
            return False, feedback, critic_rejects

        # --- 審查未全過：帶意見回饋，準備下一輪 ---
        await bc(
            events.phase_change(
                self.session_id,
                "改進討論",
                f"任務 #{task['id']} 第 {rnd} 輪未通過，工程師將依意見修正",
            )
        )
        return False, review_section, critic_rejects

    def _build_impl_prompt(
        self,
        ctx: LaneContext,
        task: dict,
        pm_plan: str,
        *,
        rnd: int,
        feedback: str,
        seed_feedback: str,
        help_used: int,
        human: str,
    ) -> str:
        """組本輪實作 prompt（首輪 vs 改進輪；含求助提示與反思記憶注入）。純組裝、無副作用。

        human 由呼叫端每輪恰好取一次（_lane_human_prefix 會 drain 插話佇列）後傳入。
        """
        # 中途求助提示：只在開關開、額度未用盡且 PM 在場時告知 marker——
        # 承諾了卻無人接（PM 缺席）比不承諾更糟，故條件含在場檢查。
        help_hint = (
            "\n若中途卡關，可輸出一行 `求助: <一句話說明卡點>`，PM 會即時給指示後你再續作。"
            if (
                config.TASK_HELP_ENABLED
                and help_used < config.TASK_HELP_MAX
                and "pm" in ctx.experts
            )
            else ""
        )
        if not feedback:
            return (
                f"{human}{self._notes_context(ctx)}"
                f"目前要完成的任務 #{task['id']}：{task['title']}\n\n"
                f"整體計畫供參考：\n{pm_plan}\n\n"
                "請在工作目錄裡實作，並在交付前自己跑過一次確認能執行。"
                f"{help_hint}"
            )
        # (A) 反思記憶：注入本任務更早輪次蒸餾的反思（最新一輪原文已在 feedback 內，故
        # exclude_latest；huddle seed＝rnd==1 且 seed_feedback，為結論非上一輪報告 → 全帶）。
        is_seed = rnd == 1 and bool(seed_feedback)
        reflections_ctx = (
            memory.build_context(self.session_id, task["id"], exclude_latest=not is_seed)
            if config.REFLEXION_ENABLED
            else ""
        )
        return (
            f"{human}{reflections_ctx}"
            f"任務 #{task['id']}：{task['title']} 尚未通過，"
            f"請根據以下意見逐項修正（第 {rnd} 輪）：\n\n{feedback}\n\n"
            "修正後請自己再跑一次確認。"
            f"{help_hint}"
        )

    async def _mid_task_help(
        self,
        ctx: LaneContext,
        task: dict,
        pm_plan: str,
        impl_role: str,
        impl_text: str,
        *,
        help_used: int,
        human: str,
        tag: int | None,
        bc: Broadcast,
    ) -> tuple[str, int]:
        """中途求助 PM 迴圈：解析 `求助:` 標記 → 問 PM → 續作覆蓋 impl_text。

        每任務至多 TASK_HELP_MAX 次（help_used 跨輪累計）；續作再含標記由上限自然擋下。
        rnd 不變、impl_history 每外輪仍只 append 最終一筆、commit 仍每輪一次 → 不影響停滯
        偵測與輪數（同 self-refine 慣例）。輪內輕量通道，與跑滿輪數才觸發的 huddle 互補。
        回傳（可能被覆蓋的 impl_text、更新後 help_used）。
        """
        while (
            config.TASK_HELP_ENABLED
            and help_used < config.TASK_HELP_MAX
            and "pm" in ctx.experts
            and not self._stop
        ):
            question = parse_help_request(impl_text)
            if not question:
                break
            help_used += 1
            await bc(
                events.phase_change(
                    self.session_id,
                    "求助PM",
                    f"任務 #{task['id']} 工程師中途求助（{help_used}/{config.TASK_HELP_MAX}）",
                )
            )
            pm_reply = await self._speak(
                ctx,
                "pm",
                f"{human}工程師實作任務 #{task['id']}：{task['title']} 時中途卡關求助：\n"
                f"{question}\n\n整體計畫供參考：\n{pm_plan}\n\n"
                "請給一段簡短、可立即執行的指示（必要時允許簡化範圍），別展開長篇討論。",
                tag,
                token_usage_task_id=task["id"],
            )
            impl_text = await self._speak(
                ctx,
                impl_role,
                f"{human}PM 對你的求助給出指示：\n{pm_reply}\n\n"
                "請依指示繼續完成本任務，完成後照常總結交付（含 `執行指令:`）。",
                tag,
                token_usage_task_id=task["id"],
            )
        return impl_text, help_used

    async def _self_refine(
        self,
        ctx: LaneContext,
        task: dict,
        impl_role: str,
        impl_text: str,
        smoke: runner.RunOutput | None,
        *,
        own_cmd: bool,
        human: str,
        tag: int | None,
        bc: Broadcast,
    ) -> tuple[str, runner.RunOutput | None]:
        """(D) 單輪內自我精修：自測「實際執行」未通過時，讓同一工程師就地依執行紀錄再修。

        訊號是 runner 的確定性 exit code（非 LLM 自評），裁決權仍在 QA/高工/客觀閘門；同一
        engineer 是有狀態對話，續一則帶 log 的訊息即可。rnd 不變、impl_history 每外輪仍只
        append 最終一筆、commit 仍每輪一次 → 不影響停滯偵測與輪數。
        觸發條件不成立時原樣回傳（impl_text, smoke），與未精修行為等價。
        """
        if (
            config.SELF_REFINE_ITERS > 0
            and smoke is not None
            and not smoke.ok
            and own_cmd
            and not self._stop
        ):
            for i in range(1, config.SELF_REFINE_ITERS + 1):
                await bc(
                    events.phase_change(
                        self.session_id,
                        "自我精修",
                        f"任務 #{task['id']} 交付前自測未通過，工程師就地修正"
                        f"（{i}/{config.SELF_REFINE_ITERS}）",
                    )
                )
                refine_prompt = (
                    f"{human}【交付前自測未通過——請先就地修正再交付】\n"
                    f"自測指令 `{smoke.command}` 實際執行未通過，紀錄如下：\n{smoke.output}\n\n"
                    "請直接修正程式碼讓它能跑過，修好後簡述改了什麼即可。"
                )
                impl_text = await self._speak(
                    ctx,
                    impl_role,
                    refine_prompt,
                    tag,
                    token_usage_task_id=task["id"],
                )
                smoke = await self._self_test(ctx, impl_text, bc)
                if smoke is None or smoke.ok:
                    break
        return impl_text, smoke

    async def _run_reviews(
        self,
        ctx: LaneContext,
        task: dict,
        pm_plan: str,
        reviewers: list[tuple[str, str]],
        *,
        rnd: int,
        tag: int | None,
        bc: Broadcast,
    ) -> tuple[bool, str]:
        """本輪三審 fan-out＋彙整：並行驗證/審查/資安 → (整體是否全過, reviewer 區段文字)。

        reviewers 由呼叫端於迴圈前算一次傳入（保「算一次」語意，不在此重算）。
        考核 perf 寫入與 run_result 廣播的先後順序原樣保留在本段內
        （tests/core/test_orchestrator_appraisal.py 直接驗證此處語意）。
        """
        await bc(
            events.phase_change(
                self.session_id,
                "驗證與審查",
                f"任務 #{task['id']} 並行驗證/審查/資安（第 {rnd} 輪）",
            )
        )
        await self._set_task_status(task, "review", bc)
        # reviewer 資料驅動：依 workflow review gate（過濾在場）並行發言。預設 qa/senior(+security
        # 在場) 用專屬 prompt → 與重構前逐字等價；客製可增刪 reviewer（含非核心角色＋verdict）。
        review_calls = [
            self._speak(
                ctx,
                rk,
                self._review_prompt(rk, vn, task, pm_plan),
                tag,
                token_usage_task_id=task["id"],
            )
            for rk, vn in reviewers
        ]
        texts = await asyncio.gather(*review_calls)
        # 每個 reviewer 的裁決＋feedback 區段標籤（未知角色用其顯示名）；裁決函式取自白名單。
        reviews = [
            {
                "role": rk,
                "ok": workflow_mod.VERDICTS[vn](txt),
                "text": txt,
                "label": _REVIEW_LABELS.get(rk, f"{ctx.experts[rk].role.name}意見"),
            }
            for (rk, vn), txt in zip(reviewers, texts, strict=True)
        ]
        all_review_ok = all(r["ok"] for r in reviews)
        # 客觀閘門退回 / 改進回饋共用的 reviewer 區段文字（保預設逐字：依序 \n\n 分隔各標籤段）。
        review_section = "\n\n".join(f"【{r['label']}】\n{r['text']}" for r in reviews if r["text"])
        # run_result 顯示燈以 qa_passed 裁決的 reviewer 為準（預設 qa）；無則用整體裁決。
        qa_review = next((r for r in reviews if r["role"] == "qa"), None)
        qa_ok = qa_review["ok"] if qa_review else all_review_ok
        # 考核客觀指標：逐輪累計 QA 輪數、逐輪覆寫裁決（以最後一輪為準；對應 reviewer
        # 缺席＝None）。與記分卡（history._derive_scorecard）互不依賴，供 _wrap_up 合併。
        senior_review = next((r for r in reviews if r["role"] == "senior"), None)
        perf = self._task_perf.setdefault(task["id"], {})
        perf["qa_rounds"] = (perf.get("qa_rounds") or 0) + 1
        perf["qa_passed"] = qa_review["ok"] if qa_review else None
        perf["senior_approved"] = senior_review["ok"] if senior_review else None
        await bc(events.run_result(self.session_id, qa_ok, "驗證通過" if qa_ok else "驗證未通過"))
        return all_review_ok, review_section

    async def _huddle_and_retry(
        self, ctx: LaneContext, task: dict, context: str, broadcast: Broadcast | None = None
    ) -> bool:
        """卡關升級：召集團隊 huddle 找替代方案 → 給 1 輪重試。

        重試仍失敗則把 task 標為「已知限制」（註記 + 事件），status 由呼叫端維持 review。
        任務路徑傳入 tagged broadcast；主 lane 只標 token_usage，並行 lane 才標全部任務事件。
        """
        bc = broadcast or self.broadcast
        conclusion = await self._huddle(ctx, task, context, bc)
        task_ok = await self._work_task(
            ctx,
            task,
            context,
            max_rounds=1,
            seed_feedback=f"【卡關 huddle 替代方案，請據此突破】\n{conclusion}",
        )
        if not task_ok:
            task["limitation"] = True
            await bc(
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
        self, ctx: LaneContext, task: dict, context: str, broadcast: Broadcast | None = None
    ) -> str:
        """召集卡關討論：讓在場角色針對 blocker 提替代方案。回傳彙整結論。

        召集 PM＋架構師＋工程師＋高級工程師（取自該 lane 的專家團隊），缺席角色
        （如 offline 無架構師）自動略過。任務路徑傳入 tagged broadcast 供前端分流。

        發言調度依 config.DISCUSS_MODE：round_robin/parallel（預設）走 DiscussionEngine
        單輪（max_rounds=1，每人剛好一次）——parallel 即同輪並行（角色同時動工）；legacy
        或在場 <2 退化時走原始循序逐行發言（逃生口）。兩條路徑的 event 形狀、participants
        鍵清單、NOTES 寫入與回傳結論皆相同。
        """
        roster = [
            ("pm", ctx.experts.get("pm")),
            ("architect", ctx.experts.get("architect")),
            ("engineer", ctx.experts.get("engineer")),
            ("senior", ctx.experts.get("senior")),
        ]
        present = [(key, ex) for key, ex in roster if ex is not None]
        bc = broadcast or self.broadcast
        await bc(
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
        ask = "請針對這個 blocker 提出可突破的替代做法或拆解方式，簡短具體、可立即執行。"
        if config.DISCUSS_MODE in ("round_robin", "parallel") and len(present) >= 2:
            # 並行/依序卡關討論：單輪（max_rounds=1）讓全員各針對 blocker 提一次替代方案。
            # 名稱進 engine（唯一且無空白）；semaphore 由 engine 內部套用（不重複包）；
            # broadcast 標籤化對齊 _speak（lane 分流）；should_stop 透傳。
            engine = DiscussionEngine(
                participants=[(ex.role.name, ex) for _, ex in present],
                mode=config.DISCUSS_MODE,
                max_rounds=1,
                semaphore=self._llm_semaphore(),
                broadcast=self._tagged_broadcast(tag, token_usage_task_id=task["id"]),
                should_stop=lambda: self._stop,
            )
            result = await engine.run(blocker + ask)
            # 依 present 順序＋{角色名: 發言} 映射回填：結論順序與 participants 鍵清單對齊
            # （engine 依 participants 順序寫回、角色名唯一 → 順序決定性，不受 gather 完成序影響）。
            by_name = {u.speaker: u.text for u in result.transcript}
            notes = [f"【{ex.role.name}】{by_name.get(ex.role.name, '')}" for _, ex in present]
        else:
            # legacy（或在場 <2 退化）：原始循序逐行發言，逃生口、行為與現狀一致。
            notes = []
            for key, ex in present:
                prior = ("\n團隊目前的討論：\n" + "\n".join(notes)) if notes else ""
                view = await self._speak(
                    ctx,
                    key,
                    blocker + ask + prior,
                    tag,
                    token_usage_task_id=task["id"],
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

    async def _store_reflection(
        self,
        ctx: LaneContext,
        task: dict,
        rnd: int,
        impl_text: str,
        feedback: str,
        bc: Broadcast,
    ) -> None:
        """(A) 某輪未通過後，把評審意見蒸餾成反思寫入 per-task 記憶，供後續輪/huddle 重試帶回。

        opt-in（REFLEXION_ENABLED）且需有 cwd（離線單元測試 cwd=None 時跳過）。反思的 LLM 呼叫
        經號誌節流、且有不崩 fallback（reflect_and_store 永不 raise），任何失敗都不影響主迴圈。
        儲存編號用「本任務已存反思數 + 1」，使主迴圈與 huddle 重試的編號單調不撞。
        """
        if not config.REFLEXION_ENABLED or not ctx.cwd:
            return
        from . import providers  # 延後 import：關閉反思時零成本，且避開 SDK 載入路徑

        await bc(
            events.phase_change(
                self.session_id,
                "反思",
                f"任務 #{task['id']} 第 {rnd} 輪未過，蒸餾反思供下一輪參考",
            )
        )
        attempt = len(memory.retrieve(self.session_id, task["id"])) + 1

        async def _llm(system: str, user: str) -> str:
            return await providers.complete_once(
                system, user, session_id=self.session_id, cwd=ctx.cwd
            )

        async with self._llm_semaphore():
            await reflexion.reflect_and_store(
                self.session_id, task, attempt, impl_text, feedback, llm=_llm
            )

    async def _self_test(
        self, ctx: LaneContext, impl_text: str, broadcast: Broadcast | None = None
    ) -> runner.RunOutput | None:
        """工程師交付前的確定性 smoke-run（在 lane 的 cwd 內執行），把完整 log 回報。

        回傳實際執行結果（供客觀閘門/自我精修判定）；無 cwd 或無可執行指令時回 None。
        任務路徑傳入 tagged broadcast；主 lane 只標 token_usage，並行 lane 才標全部任務事件。
        """
        if not ctx.cwd:
            return None
        # 工程師若宣告了 `Demo 網址:`（web 服務型產品），更新到 session 供自測/Demo 走 HTTP 路徑。
        impl_url = runner.parse_demo_url(impl_text)
        if impl_url:
            self._demo_url = impl_url
        cmd = runner.parse_run_command(impl_text) or runner.resolve_demo_command(
            ctx.cwd, self._run_command
        )
        if not cmd:
            return None
        # 刻意保留 shell（run_command，非 run_command_exec）：cmd 來自 PM/工程師宣告的
        # 自測指令（parse_run_command / resolve_demo_command 動態解析），可能含 pipe /
        # && / glob / 重導向等 shell 語法，須經 /bin/sh 解析；非固定指令、無法 argv 化。
        if self._demo_url:
            # 常駐 server 指令純 run_command 只會傻等逾時；HTTP 路徑啟動→探測→收掉。
            result, _status = await runner.run_http_demo(ctx.cwd, cmd, self._demo_url)
        else:
            result = await runner.run_command(ctx.cwd, cmd)  # nosec B602
        bc = broadcast or self.broadcast
        await bc(
            events.run_result(
                self.session_id,
                result.ok,
                f"自測 `{result.command}`：{'通過' if result.ok else '未通過'}",
                log=result.output,
            )
        )
        return result

    async def _final_demo(self) -> runner.RunOutput | None:
        """最終整體 Demo；回傳實際執行結果（供客觀閘門判定），無 cwd/指令或已停止時回 None。

        【#3 非 LLM await timeout 來源】
        - run_http_demo：timeout=config.DEMO_TIMEOUT；內部以 deadline loop 控探測上界，
          proc 收尾帶 wait_for(proc_wait, 10) + wait_for(drain_task, 5)，全段有界。
        - run_command：timeout=config.DEMO_TIMEOUT（default 60s）；底層 _finalize_proc +
          killpg，subprocess 有效強制收斂。
        - broadcast：無本地 wait_for，為無界網路 await（詳見 _integrate_wave 稽核說明）。
        """
        if not self.cwd or self._stop:
            return None
        cmd = runner.resolve_demo_command(self.cwd, self._run_command)
        if not cmd:
            return None
        if self._demo_url:
            # web 服務型產品：啟動服務 → HTTP 探測 → 收掉，讓「驗證: PASS」對網站也可信。
            await self.broadcast(
                events.phase_change(self.session_id, "Demo", f"啟動服務並探測 {self._demo_url}")
            )
            result, _status = await runner.run_http_demo(self.cwd, cmd, self._demo_url)
            await self.broadcast(
                events.demo_result(
                    self.session_id,
                    result.command,
                    result.exit_code,
                    result.output,
                    label="HTTP Demo",
                )
            )
            return result
        await self.broadcast(events.phase_change(self.session_id, "Demo", "實際執行成果"))
        # 刻意保留 shell：同 _self_test，cmd 為 demo 指令（resolve_demo_command 動態解析），
        # 可能含 shell 語法，必須經 /bin/sh，無法 argv 化。
        result = await runner.run_command(self.cwd, cmd)  # nosec B602
        retried_cmd: str | None = None
        first_exit: int | None = None
        if not result.ok:
            # usage-error 消毒重試（#248）：PM 給的 demo 指令帶了工具不認得的參數
            # （如 pytest --cache-dir → exit 4 unrecognized arguments）時，整場綠色成果
            # 會被 demo_veto 全數丟棄——指令寫壞不是產品壞。剝掉 stderr 點名的參數後
            # 重試「一次」（sanitize 回 None＝不重試），兩次嘗試都記進 demo_result 供稽核。
            # protected_text＝需求＋PM 計畫＋任務標題：要剝的參數若正是本場交付的功能
            # （如新增的 --fast-lane 旗標被拒），不得靠剝掉它讓壞交付物假綠出貨。
            protected = "\n".join(
                [self._requirement or "", self._pm_plan or ""]
                + [str(t.get("title") or "") for t in self._tasks]
            )
            sanitized = runner.sanitize_demo_command(
                cmd, result.exit_code, result.output, protected_text=protected
            )
            if sanitized:
                first_exit = result.exit_code
                retried_cmd = sanitized
                result = await runner.run_command(self.cwd, sanitized)  # nosec B602
        await self.broadcast(
            events.demo_result(
                self.session_id,
                cmd,
                result.exit_code,
                result.output,
                label="Demo",
                retried_cmd=retried_cmd,
                first_exit=first_exit,
            )
        )
        return result

    async def _wrap_up(self, pm: ExpertLike, all_ok: bool, demo_veto: bool = False) -> bool:
        await self.broadcast(events.phase_change(self.session_id, "驗收", "PM 確認驗收標準"))
        verdict = await pm.speak(
            (await self._human_prefix()) + "請依驗收標準檢查目前工作目錄的成果，判斷是否完成"
            "（輸出 `決議: 完成` 或 `決議: 未完成`；若判未完成，緊接一行 `原因: <一句根因>`，"
            "講清楚卡在哪個具體環節）。",
            self.broadcast,
        )
        done = pm_done(verdict) and all_ok and not self._stop
        # 裁決原因（(a)-lite）：未完成時抽 PM 的 `原因:` 供 autopilot 落 note；PM 沒給時，
        # 以客觀狀態合成一句兜底（all_ok/demo_veto 是機器已知的根因，不該回報空白）。
        if not done:
            reason = parse_incomplete_reason(verdict)
            if not reason:
                if demo_veto:
                    reason = "最終 Demo 實跑失敗（demo veto）"
                elif not all_ok:
                    reason = "有子任務未過三審（all_ok=False）"
                elif self._stop:
                    reason = "討論被中止"
            self._incomplete_reason = reason

        # 最終驗收放行前的異議關卡：用 senior 視角（避開剛驗收表態的 pm）。
        if done:
            critic_ok, _ = await self._critic_gate(
                self._main_ctx, "senior", "整體最終交付成果", "PM 宣告的驗收標準與整體需求"
            )
            done = critic_ok
            if not done and not self._incomplete_reason:
                self._incomplete_reason = "最終驗收異議成立（critic gate 翻盤）"

        await self.broadcast(events.phase_change(self.session_id, "檢討", "團隊進行回顧"))
        retro_prompt = (
            "請帶領團隊做一段簡短檢討：這次做得好的地方、可以改進的地方、以及後續建議。\n"
            "若過程中發現尚未解決的問題或值得改善之處，請在最後逐行列出後續任務，"
            "每行格式固定為 `後續任務: <動詞開頭的具體任務>`（沒有就不必列）；"
            "可在任務前加 `[P0/bug]` 樣式的標籤標注優先級（P0 必須~P2 加分）與類型"
            "（feature/bug/improvement），標籤可省。\n"
            "另外，若團隊判定「要滿足本需求，必須改動 Ti 核心框架本身（orchestrator／runner／"
            "發佈流程等），而非只改本專案的程式碼」，請逐行列出，格式固定為 "
            "`核心改動: <一句具體描述要改 Ti 核心的什麼>`（可加 `[P0/bug]` 標籤；沒有就不必列）。"
            "這類項目不會進本專案 repo，會被路由到 Ti 主核心 repo 另開獨立 PR。"
        )
        if config.LESSONS_ENABLED:
            retro_prompt += (
                "\n另外，若有可跨專案重用的具體經驗（踩過的坑、有效做法、技術選型結論），"
                "請逐行列出，格式固定為 `教訓: <一句精簡、可重用的經驗>`（最多 5 條，沒有就不必列）。"
            )
        if config.APPRAISAL_ENABLED:
            provs = sorted({p for p in self._role_provider_map(self._get_experts()).values() if p})
            retro_prompt += (
                "\n另外，請對每位參與的 AI 成員做績效考核，逐行輸出，格式固定為 "
                "`考核: <provider> <1-5分> <一句評語>`（分數限 1–5 整數、5 為最佳"
                + ("；本場參與 provider：" + "、".join(provs) if provs else "")
                + "）。"
            )
        retro = await pm.speak(retro_prompt, self.broadcast)
        # 考核：PM 的 `考核:` 主觀評分與本場客觀指標合併入庫＋逐筆廣播（停用/無考核行＝no-op）。
        if config.APPRAISAL_ENABLED:
            await self._record_appraisals(retro)
        # 結構化後續任務（main #95：含 priority/type）；累加而非覆寫——先前階段
        # 可能已放入後續任務，不可被檢討清掉。Demo 客觀失敗固定回填成 P0/bug。
        failed_titles = ["修復 Demo 失敗"] if demo_veto else []
        self._merge_followup_items(parse_followups_meta(retro), failed_titles=failed_titles)
        # 核心改動：判定需改 Ti 核心框架的項目，與後續任務分流——不進專案 backlog／PR，
        # 由消費端（improver／autopilot）路由到核心 backlog，autopilot 對核心 repo 開獨立 PR。
        core_seen = {c["title"] for c in self._core_changes}
        for item in parse_core_changes(retro):
            if item["title"] not in core_seen:
                core_seen.add(item["title"])
                self._core_changes.append(item)
        if self._core_changes:
            await self.broadcast(
                events.phase_change(
                    self.session_id,
                    "核心改動",
                    f"判定需改 Ti 核心 {len(self._core_changes)} 項，將路由到核心 repo（{config.CORE_REPO}）",
                )
            )
        if config.LESSONS_ENABLED:
            lessons.add_many(
                parse_lessons(retro),
                session_id=self.session_id,
                requirement=self._requirement,
            )
            # 庫超門檻時順手語意蒸餾一次（雙閘低頻、離線自動短路、壞輸出保留原庫）。
            await lessons.distill(session_id=self.session_id, cwd=self.cwd)
        # 改善計畫成果物：把本場檢討的後續改善任務＋可重用教訓沉澱成可累積的 docs/IMPROVEMENT.md
        # （比照 RESEARCH.md 知識沉澱），讓「驗證＋改善計畫」中的改善計畫成為交付物的一部分、
        # 跨場次累積，而非只進 backlog（一次性 session 的改善建議也得以持久保存）。
        self._persist_improvement_plan(retro)
        await self.broadcast(
            events.StudioEvent(events.EventType.RETROSPECTIVE, self.session_id, {"text": retro})
        )
        await self._commit(self._main_ctx, "完成：交付成果與檢討")

        files = workspace.list_files(self.workspace_id) if self.cwd else []
        await self.broadcast(
            events.StudioEvent(
                events.EventType.DONE,
                self.session_id,
                {
                    "completed": done,
                    "stopped": self._stop,
                    "files": files,
                    "parallel": self._parallel_metrics,
                },
            )
        )
        return done

    async def _record_appraisals(self, retro: str) -> None:
        """把 PM 檢討的 `考核:` 行與本場客觀指標合併，寫入考核庫並逐筆廣播 APPRAISAL。

        target 容錯兩種指認：provider 名（直接採用）或在場 role key（換算成該角色實際
        綁定的 provider，role 一併記錄）。客觀指標取「該 provider／角色本場實際做過任務」
        的聚合：qa_rounds 加總、qa_passed／senior_approved 取全數通過與否（無裁決樣本＝
        None）、duration_s 加總；沒做過任務＝各欄 None。task_id 僅在恰有一個任務吻合時
        記錄。入庫走 asyncio.to_thread、失敗只記 log——考核是旁路觀測，絕不拖垮收尾。
        """
        rows = flow.parse_appraisals(retro)
        if not rows:
            return
        role_providers = self._role_provider_map(self._get_experts())
        now = time.time()
        entries: list[dict] = []
        for r in rows:
            target = r["target"]
            role = target if target in role_providers else ""
            provider = role_providers.get(target) or ("" if role else target)
            matched = [
                (tid, m)
                for tid, m in self._task_perf.items()
                if (provider and m.get("provider") == provider) or (role and m.get("role") == role)
            ]
            qa_vals = [m.get("qa_passed") for _, m in matched if m.get("qa_passed") is not None]
            senior_vals = [
                m.get("senior_approved") for _, m in matched if m.get("senior_approved") is not None
            ]
            durations = [m["duration_s"] for _, m in matched if m.get("duration_s") is not None]
            models = [m["model"] for _, m in matched if m.get("model")]
            token_vals = [
                m["total_tokens"] for _, m in matched if m.get("total_tokens") is not None
            ]
            cost_vals = [m["cost_usd"] for _, m in matched if m.get("cost_usd") is not None]
            source_vals = [m["cost_source"] for _, m in matched if m.get("cost_source") is not None]
            cost_source = None
            if source_vals:
                cost_source = source_vals[0] if len(set(source_vals)) == 1 else "mixed"
            entries.append(
                {
                    "session_id": self.session_id,
                    "task_id": matched[0][0] if len(matched) == 1 else None,
                    "role": role,
                    "provider": provider,
                    "model": models[-1] if models else "",
                    "score": r["score"],
                    "comment": r["comment"],
                    "objective": {
                        "qa_rounds": sum(m.get("qa_rounds") or 0 for _, m in matched)
                        if matched
                        else None,
                        "qa_passed": all(qa_vals) if qa_vals else None,
                        "senior_approved": all(senior_vals) if senior_vals else None,
                        "duration_s": round(sum(durations), 1) if durations else None,
                        "total_tokens": sum(token_vals) if token_vals else None,
                        "cost_usd": sum(cost_vals) if cost_vals else None,
                        "cost_source": cost_source,
                    },
                    "created_at": now,
                }
            )
        try:
            await asyncio.to_thread(appraisal.record, entries)
        except Exception:  # noqa: BLE001 — 考核入庫失敗不得拖垮收尾
            log.warning("考核入庫失敗（略過，不影響收尾）", exc_info=True)
        for e in entries:
            await self.broadcast(
                events.appraisal(
                    self.session_id, e["provider"], e["model"], e["role"], e["score"], e["comment"]
                )
            )
        if config.LESSONS_ENABLED:
            appraisal_lessons = []
            for r in rows:
                score = r["score"]
                comment = r.get("comment", "").strip()
                if score <= 2 and comment:
                    appraisal_lessons.append(f"考核教訓({score}分): {comment}")
            if appraisal_lessons:
                try:
                    lessons.add_many(
                        appraisal_lessons,
                        session_id=self.session_id,
                        requirement=self._requirement,
                        source="appraisal",
                    )
                except Exception:  # noqa: BLE001
                    log.warning("考核教訓入庫失敗（略過，不影響收尾）", exc_info=True)

    async def _record_known_limitations(self, unmet: list[dict]) -> None:
        """帶已知限制出貨前：把未通過的次要任務寫進交付物 KNOWN_LIMITATIONS.md（隨發佈一起
        commit,讓收件方一眼看到尚未滿足之處）,並回填 followups（持續改良迴圈下次續做）。"""
        titles = [str(t.get("title", "")).strip() for t in unmet if t.get("title")]
        titles = [t for t in titles if t]
        if not self.cwd or not titles:
            return
        body = (
            "# 已知限制（Known Limitations）\n\n"
            + ("本次以「核心可用、帶已知限制」版本交付；以下項目尚未滿足,已留待後續改良:\n\n")
            + "\n".join(f"- [ ] {t}" for t in titles)
            + "\n"
        )
        try:
            (self.cwd / "KNOWN_LIMITATIONS.md").write_text(body, encoding="utf-8")
        except OSError:
            log.warning("寫入 KNOWN_LIMITATIONS.md 失敗（略過,不影響發佈）", exc_info=True)
        # 未過任務同時回填後續任務,確保不會因為「已出貨」而被遺忘。
        self._merge_followup_items([], failed_titles=titles)
        await self.broadcast(
            events.phase_change(
                self.session_id,
                "帶限制出貨",
                f"核心已通過驗證,以「已知限制」版本發佈（{len(titles)} 項未過任務記入 KNOWN_LIMITATIONS.md 並留待改良）",
            )
        )

    async def _maybe_publish(self, shippable: bool, engineer: ExpertLike | None = None) -> None:
        """專案可出貨且設定允許時自動發佈到 GitHub；接著驗 CI、失敗讓團隊修正重推、成功合併。

        首輪「等 CI→合併」沿用 publisher.publish(merge=)（REST，結局寫進 result.outcome）；CI 失敗
        則取日誌請 engineer 修正、重推，再以 verify_and_merge 重驗合併，最多 PUBLISH_CI_MAX_ROUNDS 輪。
        engineer 省略（如單測）時不進自我修復迴圈，CI 失敗即保留 PR 待人工。

        專案有自己的 publish_repo 時，整段（publish＋CI 迴圈的 verify_and_merge／
        ci_failure_logs／repush）都以 contextvar 覆寫目標 repo——同一 task 內全程生效。
        """
        try:
            token = publisher.set_repo_override(self._publish_repo)
        except ValueError as e:
            # owner allowlist 護欄攔下違規的專案 repo：以失敗的發佈結果回報，不讓例外炸掉 session。
            await self.broadcast(
                events.publish_result(
                    self.session_id, publisher.PublishResult(False, str(e)).to_dict()
                )
            )
            return
        try:
            await self._maybe_publish_inner(shippable, engineer)
        finally:
            publisher.reset_repo_override(token)

    async def _maybe_publish_inner(
        self, shippable: bool, engineer: ExpertLike | None = None
    ) -> None:
        if not self.cwd or self._stop or not shippable:
            return
        # 顯式關閉自動發佈時（如 autopilot：由其 wrapper 作為唯一發佈者，等 CI→合併），不自行發佈。
        if not self._auto_publish:
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
