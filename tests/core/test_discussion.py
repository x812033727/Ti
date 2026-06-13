"""DiscussionEngine（studio/discussion.py）的離線測試。

任務 #1 範圍：兩種模式的發言順序與輪間同步、context 餵法、semaphore 節流、
max_rounds／stalled／cancelled 停止條件、建構校驗、transcript/summary 結構。
任務 #3 範圍：TI_DISCUSS_MAX_ROUNDS 設定接入（env 解析／reload／engine 預設取用）、
stalled 提前停止標記、共識/分歧由 mentions 統計推導的小結。
任務 #5 範圍：parse_mentions @引用解析（合法案例＋格式不符退化案例）、反諂媚硬指令
入 prompt、TI_DISCUSS_MODE 分流（未設/legacy 時 `_debate` 走舊路徑一行不動；
round_robin/parallel 走 DiscussionEngine，含 ADR 蒸餾接縫與 stop 傳播）、mode env 解析。
全離線，不打真實 API。
"""

from __future__ import annotations

import asyncio

import pytest

from studio import adr, config, events
from studio.discussion import (
    DiscussionEngine,
    DiscussionResult,
    Mention,
    Utterance,
    parse_mentions,
)
from studio.orchestrator import LaneContext, StudioSession
from studio.roles import BY_KEY, Role


class StubExpert:
    """腳本化發言者：記錄收到的 prompt 與呼叫序；可選 delay 與並發探針。"""

    def __init__(self, name, texts=None, delay=0.0, sem_probe=None, order=None):
        self.name = name
        self.texts = texts
        self.calls = 0
        self.prompts: list[str] = []
        self.delay = delay
        self.sem_probe = sem_probe  # 共享 dict {"cur":0,"peak":0}：量測峰值並發
        self.order = order  # 共享 list：記錄全域發言順序

    async def speak(self, prompt, broadcast) -> str:
        self.prompts.append(prompt)
        if self.order is not None:
            self.order.append(self.name)
        if self.sem_probe is not None:
            self.sem_probe["cur"] += 1
            self.sem_probe["peak"] = max(self.sem_probe["peak"], self.sem_probe["cur"])
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.texts:
            text = self.texts[min(self.calls, len(self.texts) - 1)]
        else:
            text = f"{self.name} 第 {self.calls + 1} 輪意見（獨特內容 {self.name}-{self.calls}）"
        self.calls += 1
        if self.sem_probe is not None:
            self.sem_probe["cur"] -= 1
        return text


async def test_round_robin_order_context_and_transcript():
    order: list[str] = []
    a, b, c = (StubExpert(n, order=order) for n in ("甲", "乙", "丙"))
    eng = DiscussionEngine([("甲", a), ("乙", b), ("丙", c)], mode="round_robin", max_rounds=2)
    res = await eng.run("議題X")

    assert isinstance(res, DiscussionResult)
    # 嚴格依序發言
    assert order == ["甲", "乙", "丙", "甲", "乙", "丙"]
    assert [(u.round, u.speaker) for u in res.transcript] == [
        (1, "甲"),
        (1, "乙"),
        (1, "丙"),
        (2, "甲"),
        (2, "乙"),
        (2, "丙"),
    ]
    assert all(isinstance(u, Utterance) and u.text for u in res.transcript)
    assert res.stop_reason == "max_rounds"
    # round_robin 同輪後者可見前者：丙第 1 輪 prompt 含甲/乙第 1 輪發言
    assert "甲 第 1 輪意見" in c.prompts[0]
    assert "乙 第 1 輪意見" in c.prompts[0]
    # 第 2 輪 prompt 含議題＋上一輪全員＋自己歷史（context 餵法，不重播全史）
    assert "議題X" in a.prompts[1]
    assert "【上一輪全員發言】" in a.prompts[1]
    assert "丙 第 1 輪意見" in a.prompts[1]
    assert "【你先前的發言】" in a.prompts[1]
    assert "甲 第 1 輪意見" in a.prompts[1]
    # 第 1 輪沒有「上一輪」段落
    assert "【上一輪全員發言】" not in a.prompts[0]
    # summary 結構
    assert set(res.summary) == {"consensus", "disagreements", "final_positions"}
    assert res.summary["final_positions"]["丙"] == res.transcript[-1].text


async def test_parallel_snapshot_barrier_and_throttle():
    probe = {"cur": 0, "peak": 0}
    stubs = [StubExpert(n, delay=0.05, sem_probe=probe) for n in ("甲", "乙", "丙", "丁")]
    sem = asyncio.Semaphore(2)  # 模擬 TI_LLM_MAX_CONCURRENCY=2
    eng = DiscussionEngine(
        [(s.name, s) for s in stubs], mode="parallel", max_rounds=2, semaphore=sem
    )
    res = await eng.run("議題Y")

    # 節流：峰值並發 ≤ 注入的 semaphore 額度
    assert probe["peak"] <= 2
    for s in stubs:
        p2 = s.prompts[1]
        # 同一輪全員拿到同一份上一輪 transcript（含全部 4 人第 1 輪發言）
        for other in stubs:
            assert f"{other.name} 第 1 輪意見" in p2
        # 輪間同步屏障：第 2 輪 prompt 絕不含任何人的第 2 輪發言
        assert "第 2 輪意見" not in p2
    # 寫回固定依 participants 順序
    assert [(u.round, u.speaker) for u in res.transcript] == [
        (1, "甲"),
        (1, "乙"),
        (1, "丙"),
        (1, "丁"),
        (2, "甲"),
        (2, "乙"),
        (2, "丙"),
        (2, "丁"),
    ]
    assert res.stop_reason == "max_rounds"


async def test_max_rounds_exact_stop():
    stubs = [StubExpert(n) for n in ("甲", "乙", "丙")]
    eng = DiscussionEngine([(s.name, s) for s in stubs], mode="parallel", max_rounds=2)
    res = await eng.run("T")
    assert all(s.calls == 2 for s in stubs)
    assert max(u.round for u in res.transcript) == 2


async def test_stalled_early_stop():
    # 每輪講一模一樣的話 → 連續輪次相似度過高，max_rounds=5 提前停
    stubs = [StubExpert(n, texts=["完全相同的發言"]) for n in ("甲", "乙", "丙")]
    eng = DiscussionEngine([(s.name, s) for s in stubs], mode="round_robin", max_rounds=5)
    res = await eng.run("T")
    assert res.stop_reason == "stalled"
    assert max(u.round for u in res.transcript) == 2


async def test_should_stop_cancelled():
    stubs = [StubExpert(n) for n in ("甲", "乙", "丙")]
    eng = DiscussionEngine(
        [(s.name, s) for s in stubs], mode="parallel", max_rounds=5, should_stop=lambda: True
    )
    res = await eng.run("T")
    assert res.stop_reason == "cancelled"
    assert res.transcript == []


# --- 任務 #3：收斂控制（TI_DISCUSS_MAX_ROUNDS）與討論小結 -------------------


async def test_default_max_rounds_from_config(monkeypatch):
    """未顯式給 max_rounds 時，engine 建構當下取 config.DISCUSS_MAX_ROUNDS，恰好 N 輪停。"""
    monkeypatch.setattr(config, "DISCUSS_MAX_ROUNDS", 2)
    stubs = [StubExpert(n) for n in ("甲", "乙", "丙")]
    eng = DiscussionEngine([(s.name, s) for s in stubs], mode="round_robin")
    res = await eng.run("T")
    assert all(s.calls == 2 for s in stubs)
    assert max(u.round for u in res.transcript) == 2
    assert res.stop_reason == "max_rounds"


def test_explicit_max_rounds_overrides_config(monkeypatch):
    monkeypatch.setattr(config, "DISCUSS_MAX_ROUNDS", 7)
    s = StubExpert("甲")
    eng = DiscussionEngine([("甲", s)], max_rounds=3)
    assert eng._max_rounds == 3


def test_discuss_max_rounds_env_parsing(monkeypatch):
    """TI_DISCUSS_MAX_ROUNDS 的 env 解析：合法值生效；未設/留空/非法/<1 退回 DEBATE_ROUNDS。"""
    try:
        monkeypatch.setenv("TI_DEBATE_ROUNDS", "3")
        monkeypatch.setenv("TI_DISCUSS_MAX_ROUNDS", "5")
        config.reload()
        assert config.DISCUSS_MAX_ROUNDS == 5

        for bad in ("", "  ", "abc", "0", "-2"):
            monkeypatch.setenv("TI_DISCUSS_MAX_ROUNDS", bad)
            config.reload()
            assert config.DISCUSS_MAX_ROUNDS == config.DEBATE_ROUNDS == 3, bad

        monkeypatch.delenv("TI_DISCUSS_MAX_ROUNDS")
        config.reload()
        assert config.DISCUSS_MAX_ROUNDS == 3
    finally:
        monkeypatch.undo()
        config.reload()  # 還原全域，避免污染其他測試


async def test_stalled_marks_reason_and_summary_structure():
    """stalled 提前停止：stop_reason 標記正確，且小結三鍵齊備、final_positions 取末輪發言。"""
    stubs = [StubExpert(n, texts=["重複立場，無新進展"]) for n in ("甲", "乙", "丙")]
    eng = DiscussionEngine([(s.name, s) for s in stubs], mode="parallel", max_rounds=6)
    res = await eng.run("T")
    assert res.stop_reason == "stalled"
    assert set(res.summary) == {"consensus", "disagreements", "final_positions"}
    assert res.summary["final_positions"] == {n: "重複立場，無新進展" for n in ("甲", "乙", "丙")}


def test_summary_consensus_and_disagreements_from_mentions():
    """共識/分歧由 mentions 統計推導：同意進 consensus、反對進 disagreements、
    同一對先同意後反對以分歧為準（agree - disagree）。"""
    s = StubExpert("甲")
    eng = DiscussionEngine([("甲", s)], max_rounds=1)
    transcript = [
        Utterance(1, "甲", "支持乙", [Mention("甲", "乙", "同意")]),
        Utterance(1, "乙", "反對丙", [Mention("乙", "丙", "反對")]),
        Utterance(1, "丙", "先同意", [Mention("丙", "甲", "同意")]),
        Utterance(2, "丙", "改反對", [Mention("丙", "甲", "反對")]),
    ]
    summary = eng._build_summary(transcript)
    assert summary["consensus"] == ["甲 同意 乙"]
    assert set(summary["disagreements"]) == {"乙 反對 丙", "丙 反對 甲"}
    assert summary["disagreements"] == sorted(summary["disagreements"])  # 穩定排序可重現
    assert "丙 同意 甲" not in summary["consensus"]  # 立場翻轉以分歧為準
    assert summary["final_positions"]["丙"] == "改反對"


def test_constructor_validation():
    s = StubExpert("甲")
    with pytest.raises(ValueError):
        DiscussionEngine([("甲", s), ("甲", s)])  # 重名
    with pytest.raises(ValueError):
        DiscussionEngine([("有 空白", s)])  # 名稱含空白
    with pytest.raises(ValueError):
        DiscussionEngine([("", s)])  # 空名稱
    with pytest.raises(ValueError):
        DiscussionEngine([])  # 空清單
    with pytest.raises(ValueError):
        DiscussionEngine([("甲", s)], mode="moderator")  # 不支援的 mode
    with pytest.raises(ValueError):
        DiscussionEngine([("甲", s)], max_rounds=0)  # 壞輪數


# --- 任務 #5：parse_mentions @引用解析（含格式不符退化案例）-------------------

NAMES = ("工程師", "高級工程師", "架構師")


def test_parse_mentions_valid_cases():
    text = (
        "我的看法如下。\n"
        "回應 @高級工程師: 同意 ＋並發模型合理\n"
        "回應 @架構師：反對 ＋輪數上限太低有風險\n"  # 全形冒號也要收
        "回應 @高級工程師 : 反對 ＋名稱與冒號間可有空白"
    )
    got = parse_mentions("工程師", text, NAMES)
    assert got == [
        Mention("工程師", "高級工程師", "同意"),
        Mention("工程師", "架構師", "反對"),
        Mention("工程師", "高級工程師", "反對"),
    ]


def test_parse_mentions_prefix_name_no_misalign():
    # 短名稱是長名稱前綴（工程師 vs 高級工程師）：必須匹配到完整長名稱，不可錯位截斷
    got = parse_mentions("架構師", "回應 @高級工程師: 同意 ＋理由", NAMES)
    assert got == [Mention("架構師", "高級工程師", "同意")]


@pytest.mark.parametrize(
    "bad",
    [
        "回應 @高級工程師 同意 ＋缺冒號",  # 缺冒號
        "回應 @ 高級工程師: 同意 ＋@ 與名稱間有空白（緊鄰格式不符）",
        "回應 @高級工程師: 中立 ＋立場詞不在二值白名單",  # 立場非 同意|反對
        "回應 @路人甲: 同意 ＋target 不在名單",  # 白名單外角色
        "@高級工程師: 同意 ＋缺「回應」前綴",  # 缺前綴
        "我大致同意高級工程師的看法",  # 自由文字，無結構化引用
        "",  # 空字串
    ],
)
def test_parse_mentions_malformed_returns_empty(bad):
    """格式不符整段視為無引用：回空清單，絕不產生錯位結果。"""
    assert parse_mentions("工程師", bad, NAMES) == []


def test_parse_mentions_self_mention_dropped_others_kept():
    text = "回應 @工程師: 同意 ＋自我引用應丟棄\n回應 @架構師: 反對 ＋這條要保留"
    assert parse_mentions("工程師", text, NAMES) == [Mention("工程師", "架構師", "反對")]


def test_parse_mentions_empty_participants():
    assert parse_mentions("工程師", "回應 @工程師: 同意", []) == []
    assert parse_mentions("工程師", "回應 @x: 同意", ["", ""]) == []


async def test_engine_fills_mentions_and_anti_sycophancy_prompt():
    """整合面：engine 把 parse_mentions 結果填進 Utterance.mentions、
    匯入 summary 共識/分歧；且每位角色每輪 prompt 都含反諂媚硬指令與結構化引用格式。"""
    a = StubExpert("甲", texts=["回應 @乙: 同意 ＋方向正確"])
    b = StubExpert("乙", texts=["回應 @甲: 反對 ＋缺測試"])
    c = StubExpert("丙", texts=["自由發言，沒有結構化引用"])
    eng = DiscussionEngine([("甲", a), ("乙", b), ("丙", c)], mode="round_robin", max_rounds=1)
    res = await eng.run("T")
    by_speaker = {u.speaker: u for u in res.transcript}
    assert by_speaker["甲"].mentions == [Mention("甲", "乙", "同意")]
    assert by_speaker["乙"].mentions == [Mention("乙", "甲", "反對")]
    assert by_speaker["丙"].mentions == []  # 格式不符退化：空清單
    assert res.summary["consensus"] == ["甲 同意 乙"]
    assert res.summary["disagreements"] == ["乙 反對 甲"]
    for stub in (a, b, c):
        for prompt in stub.prompts:
            assert "回應 @角色名: 同意 ＋理由" in prompt  # 結構化引用格式
            assert "至少指出一個可挑戰點" in prompt  # 反諂媚硬指令
            assert "反諂媚" in prompt


# --- 任務 #5：TI_DISCUSS_MODE 分流（_debate legacy vs DiscussionEngine）------


class RoleStub:
    """帶 role 的腳本化專家（engine 路徑用 a.role.name 組 participants）。"""

    def __init__(self, role: Role, scripts: list[str]):
        self.role = role
        self._scripts = scripts
        self.calls = 0
        self.prompts: list[str] = []

    async def speak(self, prompt: str, broadcast) -> str:
        self.prompts.append(prompt)
        text = self._scripts[min(self.calls, len(self._scripts) - 1)]
        self.calls += 1
        return text


def _collect():
    bucket: list[events.StudioEvent] = []

    async def broadcast(ev: events.StudioEvent) -> None:
        bucket.append(ev)

    return bucket, broadcast


def _phases(bucket):
    return [
        (e.payload["phase"], e.payload["detail"])
        for e in bucket
        if e.type == events.EventType.PHASE_CHANGE
    ]


async def test_debate_legacy_path_when_mode_legacy(monkeypatch):
    """DISCUSS_MODE=legacy（opt-out 逃生口）→ _debate 走舊「提案→點評」路徑，
    絕不建構 DiscussionEngine。預設已是 parallel，此處顯式 pin legacy 驗逃生口。"""
    monkeypatch.setattr(config, "DISCUSS_MODE", "legacy")

    import studio.orchestrator as orch

    class Boom:
        def __init__(self, *a, **k):
            raise AssertionError("legacy 路徑不應建構 DiscussionEngine")

    monkeypatch.setattr(orch, "DiscussionEngine", Boom)
    monkeypatch.setattr(config, "ADR_ENABLED", False)
    bucket, broadcast = _collect()
    eng = RoleStub(BY_KEY["engineer"], ["提案內容"])
    senior = RoleStub(BY_KEY["senior"], ["點評內容"])
    session = StudioSession("t", broadcast, experts=None, cwd=None)
    await session._debate(eng, senior, "議題L", rounds=1)

    assert eng.calls == 1 and senior.calls == 1  # 舊路徑：a 提案、b 點評各一次
    assert "請先簡短提出你打算採取的整體做法" in eng.prompts[0]
    assert "提案內容" in senior.prompts[0]
    assert ("架構討論", "工程師與高級工程師對齊做法") in _phases(bucket)


async def test_debate_engine_path_round_robin(monkeypatch):
    """DISCUSS_MODE=round_robin → 走 DiscussionEngine：兩角色各發言 DISCUSS_MAX_ROUNDS 輪、
    phase 事件標明模式、prompt 含反諂媚硬指令。"""
    monkeypatch.setattr(config, "DISCUSS_MODE", "round_robin")
    monkeypatch.setattr(config, "DISCUSS_MAX_ROUNDS", 2)
    monkeypatch.setattr(config, "ADR_ENABLED", False)
    bucket, broadcast = _collect()
    eng = RoleStub(BY_KEY["engineer"], ["工程師第一輪", "工程師第二輪"])
    senior = RoleStub(BY_KEY["senior"], ["高工第一輪", "高工第二輪"])
    # 傳 stub experts dict：engine 路徑會取 _llm_semaphore()，experts=None 會真建 SDK 專家
    session = StudioSession("t", broadcast, experts={"engineer": eng, "senior": senior}, cwd=None)
    await session._debate(eng, senior, "議題E", rounds=1)

    assert eng.calls == 2 and senior.calls == 2  # 各 2 輪；ADR 關閉無蒸餾加場
    assert ("架構討論", "多角色討論（round_robin）對齊做法") in _phases(bucket)
    assert "議題E" in eng.prompts[0]
    assert "至少指出一個可挑戰點" in eng.prompts[0]  # 反諂媚硬指令進了 engine prompt
    assert "工程師第一輪" in senior.prompts[0]  # round_robin 同輪後者可見前者


async def test_debate_engine_parallel_distills_adr(tmp_path, monkeypatch):
    """engine 路徑的 ADR 蒸餾接縫：蒸餾 prompt 餵 final_positions＋末輪 transcript，
    沿用同一蒸餾指令與 adr.record 落盤。"""
    monkeypatch.setattr(config, "DISCUSS_MODE", "parallel")
    monkeypatch.setattr(config, "DISCUSS_MAX_ROUNDS", 1)
    monkeypatch.setattr(config, "ADR_ENABLED", True)
    _, broadcast = _collect()
    eng = RoleStub(BY_KEY["engineer"], ["工程師立場：用引擎"])
    senior = RoleStub(
        BY_KEY["senior"], ["高工立場：同意引擎", "決策: 討論層走 DiscussionEngine\n理由: 可測可擴"]
    )
    session = StudioSession(
        "t", broadcast, experts={"engineer": eng, "senior": senior}, cwd=tmp_path
    )
    # _commit 需要 main lane ctx；給 cwd=None 的空 ctx 讓它安全短路（不真跑 git）。
    session._main_ctx = LaneContext(lane_id="main", cwd=None, experts={})
    await session._debate(eng, senior, "議題A", rounds=1)

    assert eng.calls == 1  # 1 輪發言
    assert senior.calls == 2  # 1 輪發言＋1 次蒸餾
    distill = senior.prompts[-1]
    assert "蒸餾成決策記錄" in distill  # 沿用舊路徑同一蒸餾指令
    assert "最終立場】" in distill and "【末輪發言】" in distill
    assert "工程師立場：用引擎" in distill and "高工立場：同意引擎" in distill
    entries = adr.all_entries(tmp_path)
    assert [e["decision"] for e in entries] == ["討論層走 DiscussionEngine"]
    assert entries[0]["rationale"] == "可測可擴"


async def test_debate_engine_stop_propagates(tmp_path, monkeypatch):
    """should_stop 接 self._stop：發言中途停止 → 討論取消、ADR 蒸餾不執行、不落盤。"""
    monkeypatch.setattr(config, "DISCUSS_MODE", "round_robin")
    monkeypatch.setattr(config, "DISCUSS_MAX_ROUNDS", 5)
    monkeypatch.setattr(config, "ADR_ENABLED", True)
    _, broadcast = _collect()
    senior = RoleStub(BY_KEY["senior"], ["不該被叫到", "決策: 不該落盤"])
    session = StudioSession("t", broadcast, experts={"senior": senior}, cwd=tmp_path)
    session._main_ctx = LaneContext(lane_id="main", cwd=None, experts={})

    class StopperStub(RoleStub):
        async def speak(self, prompt, broadcast):
            text = await super().speak(prompt, broadcast)
            session._stop = True  # 模擬使用者於發言期間按停止
            return text

    eng = StopperStub(BY_KEY["engineer"], ["說完即停"])
    await session._debate(eng, senior, "議題S", rounds=1)

    assert eng.calls == 1 and senior.calls == 0  # 同輪下一位即被攔下
    assert not (tmp_path / "adr.json").exists()  # cancelled 後不蒸餾、不落盤


def test_discuss_mode_env_parsing(monkeypatch):
    """TI_DISCUSS_MODE 的 env 解析：白名單值生效；未設/留空＝採新預設 parallel；
    非空但拼錯/大小寫不符＝安全退回 legacy（絕不誤開新路徑）。"""
    try:
        for good in ("round_robin", "parallel", "legacy"):
            monkeypatch.setenv("TI_DISCUSS_MODE", good)
            config.reload()
            assert config.DISCUSS_MODE == good
        # 留空／純空白 ＝「未設定」＝ 採新預設 parallel（沿用 .env 留空慣例）
        for blank in ("", "  "):
            monkeypatch.setenv("TI_DISCUSS_MODE", blank)
            config.reload()
            assert config.DISCUSS_MODE == "parallel", repr(blank)
        # 非空但非法（拼錯／大小寫／round-robin）＝ 安全退回 legacy
        for bad in ("moderator", "PARALLEL", "round-robin"):
            monkeypatch.setenv("TI_DISCUSS_MODE", bad)
            config.reload()
            assert config.DISCUSS_MODE == "legacy", bad
        # 未設 ＝ 採新預設 parallel
        monkeypatch.delenv("TI_DISCUSS_MODE")
        config.reload()
        assert config.DISCUSS_MODE == "parallel"
    finally:
        monkeypatch.undo()
        config.reload()  # 還原全域，避免污染其他測試
