"""任務 #3：拆解 prompt 議程化＋orchestrator 逐子題討論呼叫端（離線測試）。

涵蓋：
- AGENDA_PROMPT_RULES 粒度守則 micro-rules（字面 grep 驗證）＋ role_key 白名單注入。
- TI_AGENDA_ROUNDS env 解析（未設/合法/非法/<1 一律安全 fallback 1）與 reload 接入。
- _run 拆解後議程解析＋assignee 硬驗證（合法照分派／非法 fallback engineer），
  legacy 模式下 DiscussionEngine 絕不被建構（零回歸）。
- _discuss_agenda：多子題逐子題餵 DiscussionEngine（topic=標題＋描述）、assignee 取得
  先發言權、多子題輪數走 AGENDA_ROUNDS、ADR 蒸餾收斂為一次（不逐子題蒸餾）。
- 引擎模式 full _run：呼叫端分流到逐子題討論（phase 事件＋子題 topic 可回指輸入）。
全離線，不打真實 API。
"""

from __future__ import annotations

import pytest

from studio import adr, config, events
from studio.orchestrator import AGENDA_PROMPT_RULES, LaneContext, StudioSession
from studio.roles import BY_KEY, Role


class StubExpert:
    """腳本化專家：記錄 prompts／呼叫數；order 共享 list 記錄全域發言順序。"""

    def __init__(self, role: Role, scripts: list[str], order: list | None = None):
        self.role = role
        self._scripts = scripts
        self.calls = 0
        self.prompts: list[str] = []
        self.order = order

    async def speak(self, prompt: str, broadcast) -> str:
        self.prompts.append(prompt)
        if self.order is not None:
            self.order.append(self.role.key)
        text = self._scripts[min(self.calls, len(self._scripts) - 1)]
        self.calls += 1
        return text

    async def stop(self) -> None:
        pass


def collect():
    bucket: list[events.StudioEvent] = []

    async def broadcast(ev: events.StudioEvent) -> None:
        bucket.append(ev)

    return bucket, broadcast


def phases(bucket):
    return [
        (e.payload["phase"], e.payload["detail"])
        for e in bucket
        if e.type == events.EventType.PHASE_CHANGE
    ]


# --- 拆解 prompt：粒度守則 micro-rules ------------------------------------


def test_agenda_prompt_rules_microrules():
    """驗收標準 4：粒度守則字面入 prompt，可直接 grep 字串驗證。"""
    assert "子題 2–5 個" in AGENDA_PROMPT_RULES
    assert "每任務一句可驗收" in AGENDA_PROMPT_RULES
    assert "探索型議題允許單子題、不硬拆" in AGENDA_PROMPT_RULES
    assert "子題: <標題> | <一句描述> | <成功準則>" in AGENDA_PROMPT_RULES
    assert "負責: <role_key>" in AGENDA_PROMPT_RULES
    # 並行引導(第五輪 P2):任務/依賴語法必須明確定義(舊版寫「照上述格式」但上文沒定義,
    # PM 慣性輸出線性依賴鏈→實測 lanes_max 幾乎全為 1),並要求獨立子任務優先。
    assert "任務: #<id> <標題>" in AGENDA_PROMPT_RULES
    assert "依賴: #<後> -> #<前>" in AGENDA_PROMPT_RULES
    assert "依賴僅在真有產出先後時才標" in AGENDA_PROMPT_RULES
    assert "不要另立「補測試」「複核」尾任務串成流水線" in AGENDA_PROMPT_RULES
    assert "互不依賴" in AGENDA_PROMPT_RULES and "同一波次並行執行" in AGENDA_PROMPT_RULES
    # {keys} 注入本場出席角色白名單
    formatted = AGENDA_PROMPT_RULES.format(keys="pm, engineer, senior")
    assert "限定下列其一：pm, engineer, senior" in formatted


# --- TI_AGENDA_ROUNDS env 解析 --------------------------------------------


def test_agenda_rounds_env_parsing(monkeypatch):
    """未設/留空→1；合法整數生效；非整數/<1 一律 fallback 1（絕不丟例外）。"""
    try:
        monkeypatch.delenv("TI_AGENDA_ROUNDS", raising=False)
        config.reload()
        assert config.AGENDA_ROUNDS == 1
        monkeypatch.setenv("TI_AGENDA_ROUNDS", "3")
        config.reload()
        assert config.AGENDA_ROUNDS == 3
        for bad in ("abc", "0", "-2", " "):
            monkeypatch.setenv("TI_AGENDA_ROUNDS", bad)
            config.reload()
            assert config.AGENDA_ROUNDS == 1
    finally:
        monkeypatch.delenv("TI_AGENDA_ROUNDS", raising=False)
        config.reload()


# --- _run 接線：議程解析＋assignee 硬驗證（legacy 零回歸） -------------------

PM_PLAN_WITH_AGENDA = (
    "子題: 資料層 | 設計儲存格式 | 可離線讀寫\n"
    "負責: senior\n"
    "子題: 介面層 | 設計 CLI 參數 | 一鍵可跑\n"
    "負責: ghost\n"
    "任務: #1 實作資料層\n"
    "執行指令: python main.py"
)


def _experts(pm_scripts):
    return {
        "pm": StubExpert(BY_KEY["pm"], pm_scripts),
        "engineer": StubExpert(BY_KEY["engineer"], ["已實作"]),
        "qa": StubExpert(BY_KEY["qa"], ["驗證: PASS"]),
        "senior": StubExpert(BY_KEY["senior"], ["決議: 核可"]),
    }


@pytest.mark.asyncio
async def test_run_parses_agenda_and_hard_validates_assignee(monkeypatch):
    """驗收標準 3：合法 key（senior）照分派；非法 key（ghost）fallback engineer。
    legacy 模式下 DiscussionEngine 絕不被建構（零回歸）；PM 拆解 prompt 含粒度守則。"""
    import studio.orchestrator as orch

    class Boom:
        def __init__(self, *a, **k):
            raise AssertionError("legacy 路徑不應建構 DiscussionEngine")

    monkeypatch.setattr(orch, "DiscussionEngine", Boom)
    monkeypatch.setattr(config, "DEBATE_ROUNDS", 0)
    # 預設已改 parallel（#115）；顯式 pin legacy 驗「逃生口」路徑零回歸（不建構 DiscussionEngine）。
    monkeypatch.setattr(config, "DISCUSS_MODE", "legacy")
    bucket, broadcast = collect()
    experts = _experts([PM_PLAN_WITH_AGENDA, "決議: 完成", "檢討 OK"])
    session = StudioSession("t", broadcast, experts=experts, cwd=None)
    await session.run("做一個記帳 CLI")

    # PM 拆解 prompt 收到議程守則＋本場 role_key 白名單
    pm_prompt = experts["pm"].prompts[0]
    assert "子題 2–5 個" in pm_prompt and "每任務一句可驗收" in pm_prompt
    assert "pm, engineer, qa, senior" in pm_prompt
    # 議程解析回指本次輸入；assignee 硬驗證
    assert [(a["title"], a["assignee"]) for a in session._agenda] == [
        ("資料層", "senior"),
        ("介面層", "engineer"),  # ghost 非法 → fallback engineer
    ]
    assert session._agenda_corrections == [{"index": 1, "given": "ghost", "assigned": "engineer"}]


@pytest.mark.asyncio
async def test_run_without_agenda_lines_falls_back_single_item(monkeypatch):
    """無 `子題:` 行（舊式 PM 輸出）→ 單一子題＝原需求全文，不噴錯（零回歸）。"""
    monkeypatch.setattr(config, "DEBATE_ROUNDS", 0)
    _, broadcast = collect()
    experts = _experts(["任務: 實作 BMI\n執行指令: python main.py", "決議: 完成", "OK"])
    session = StudioSession("t", broadcast, experts=experts, cwd=None)
    await session.run("做一個 BMI CLI")
    assert [a["title"] for a in session._agenda] == ["做一個 BMI CLI"]
    assert session._agenda[0]["assignee"] == "engineer"  # 缺漏 fallback


# --- _discuss_agenda：逐子題引擎呼叫 ---------------------------------------


def _session_with_agenda(broadcast, agenda, experts, cwd=None):
    session = StudioSession("t", broadcast, experts=experts, cwd=cwd)
    session._agenda = agenda
    session._main_ctx = LaneContext(lane_id="main", cwd=None, experts={})
    return session


@pytest.mark.asyncio
async def test_discuss_agenda_per_subtopic_proposer_first(monkeypatch):
    """驗收標準 5：多子題時 DiscussionEngine 被逐子題呼叫（topic=標題＋描述），
    assignee 取得先發言權（提案方），多子題輪數走 AGENDA_ROUNDS=1。"""
    monkeypatch.setattr(config, "DISCUSS_MODE", "round_robin")
    monkeypatch.setattr(config, "AGENDA_ROUNDS", 1)
    monkeypatch.setattr(config, "DEBATE_ROUNDS", 1)
    monkeypatch.setattr(config, "ADR_ENABLED", False)
    order: list[str] = []
    eng = StubExpert(BY_KEY["engineer"], ["工程師意見"], order=order)
    senior = StubExpert(BY_KEY["senior"], ["高工意見"], order=order)
    experts = {"engineer": eng, "senior": senior}
    bucket, broadcast = collect()
    agenda = [
        {
            "title": "資料層",
            "description": "設計儲存格式",
            "criteria": "可離線",
            "assignee": "senior",
        },
        {"title": "介面層", "description": "設計 CLI 參數", "criteria": "", "assignee": "engineer"},
    ]
    session = _session_with_agenda(broadcast, agenda, experts)
    note = await session._discuss_agenda(experts, eng, senior, "做一個記帳 CLI")

    # 逐子題各 1 輪：子題1 主責 senior 先發言、子題2 主責 engineer 先發言
    assert order == ["senior", "engineer", "engineer", "senior"]
    # topic=標題＋描述（自證回指輸入）＋主責標記
    assert "議程子題 1/2：資料層" in senior.prompts[0]
    assert "描述: 設計儲存格式" in senior.prompts[0]
    assert "成功準則: 可離線" in senior.prompts[0]
    assert "主責: 高級工程師" in senior.prompts[0]
    assert "議程子題 2/2：介面層" in eng.prompts[1]
    assert "主責: 工程師" in eng.prompts[1]
    # design_note 串接各子題 final_positions
    assert "〔子題 1：資料層〕" in note and "〔子題 2：介面層〕" in note
    assert ("架構討論", "逐子題多角色討論（round_robin，2 個子題）") in phases(bucket)


@pytest.mark.asyncio
async def test_discuss_agenda_single_distill_and_commit(tmp_path, monkeypatch):
    """ADR 蒸餾收斂為一次：兩個子題討論完只做一次蒸餾、一筆 adr.record（不逐子題蒸餾）。"""
    monkeypatch.setattr(config, "DISCUSS_MODE", "round_robin")
    monkeypatch.setattr(config, "AGENDA_ROUNDS", 1)
    monkeypatch.setattr(config, "DEBATE_ROUNDS", 1)
    monkeypatch.setattr(config, "ADR_ENABLED", True)
    eng = StubExpert(BY_KEY["engineer"], ["工程師立場"])
    # senior 發言序：子題1、子題2、結論蒸餾、ADR 蒸餾（結論在 ADR 前，見 _discuss_agenda）。
    # 結論蒸餾須給一個獨立腳本，避免搶走 ADR 的 `決策:` 腳本（高工退回點）。
    senior = StubExpert(
        BY_KEY["senior"],
        [
            "高工立場",
            "高工立場",
            "共識: 工程師 同意 高級工程師",
            "決策: 資料層用 JSONL\n理由: 簡單可附加",
        ],
    )
    experts = {"engineer": eng, "senior": senior}
    _, broadcast = collect()
    agenda = [
        {"title": "資料層", "description": "", "criteria": "", "assignee": "engineer"},
        {"title": "介面層", "description": "", "criteria": "", "assignee": "engineer"},
    ]
    session = _session_with_agenda(broadcast, agenda, experts, cwd=tmp_path)
    await session._discuss_agenda(experts, eng, senior, "做一個記帳 CLI")

    assert eng.calls == 2  # 2 子題 × 1 輪
    assert senior.calls == 4  # 2 子題發言＋1 次結論蒸餾＋1 次 ADR 蒸餾
    # 結論蒸餾排在 ADR 之前（prompts[2]），不得搶走 ADR 的輸入。
    assert "結構化結論" in senior.prompts[2]
    distill = senior.prompts[-1]  # 最後一次仍是 ADR 蒸餾
    assert "蒸餾成決策記錄" in distill
    assert "〔子題 1：資料層〕" in distill and "〔子題 2：介面層〕" in distill
    # ADR 仍拿到正確的 `決策:` 輸入、只記一次（結論蒸餾未污染）。
    assert [e["decision"] for e in adr.all_entries(tmp_path)] == ["資料層用 JSONL"]
    # 結論彙整副產物：CONCLUSION.md 確實落盤（接線生效）。
    assert (tmp_path / "CONCLUSION.md").is_file()


@pytest.mark.asyncio
async def test_discuss_agenda_skipped_when_debate_rounds_zero(monkeypatch):
    """TI_DEBATE_ROUNDS=0 → 議程討論整段跳過（與 _debate rounds<=0 語意一致）。"""
    monkeypatch.setattr(config, "DISCUSS_MODE", "round_robin")
    monkeypatch.setattr(config, "DEBATE_ROUNDS", 0)
    eng = StubExpert(BY_KEY["engineer"], ["不該被叫到"])
    senior = StubExpert(BY_KEY["senior"], ["不該被叫到"])
    experts = {"engineer": eng, "senior": senior}
    _, broadcast = collect()
    agenda = [{"title": "唯一子題", "description": "", "criteria": "", "assignee": "engineer"}]
    session = _session_with_agenda(broadcast, agenda, experts)
    note = await session._discuss_agenda(experts, eng, senior, "需求")
    assert note == "" and eng.calls == 0 and senior.calls == 0


@pytest.mark.asyncio
async def test_run_engine_mode_routes_to_per_subtopic_discussion(monkeypatch):
    """引擎模式 full _run：呼叫端分流到逐子題討論——phase 事件標明逐子題、
    子題 topic 進到討論 prompt（回指本次 PM 輸出，排除假綠）。"""
    monkeypatch.setattr(config, "DISCUSS_MODE", "round_robin")
    monkeypatch.setattr(config, "AGENDA_ROUNDS", 1)
    monkeypatch.setattr(config, "DEBATE_ROUNDS", 1)
    monkeypatch.setattr(config, "ADR_ENABLED", False)
    bucket, broadcast = collect()
    experts = _experts([PM_PLAN_WITH_AGENDA, "決議: 完成", "檢討 OK"])
    session = StudioSession("t", broadcast, experts=experts, cwd=None)
    await session.run("做一個記帳 CLI")

    assert ("架構討論", "逐子題多角色討論（round_robin，2 個子題）") in phases(bucket)
    senior_prompts = "\n".join(experts["senior"].prompts)
    assert "議程子題 1/2：資料層" in senior_prompts
    assert "議程子題 2/2：介面層" in senior_prompts
