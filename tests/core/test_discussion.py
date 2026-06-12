"""DiscussionEngine（studio/discussion.py）的離線測試。

任務 #1 範圍：兩種模式的發言順序與輪間同步、context 餵法、semaphore 節流、
max_rounds／stalled／cancelled 停止條件、建構校驗、transcript/summary 結構。
任務 #3 範圍：TI_DISCUSS_MAX_ROUNDS 設定接入（env 解析／reload／engine 預設取用）、
stalled 提前停止標記、共識/分歧由 mentions 統計推導的小結。
（任務 #5 將再擴充 @引用解析與 TI_DISCUSS_MODE 分流案例。）
"""

from __future__ import annotations

import asyncio

import pytest

from studio import config
from studio.discussion import DiscussionEngine, DiscussionResult, Mention, Utterance


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
        (1, "甲"), (1, "乙"), (1, "丙"), (2, "甲"), (2, "乙"), (2, "丙"),
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
        (1, "甲"), (1, "乙"), (1, "丙"), (1, "丁"),
        (2, "甲"), (2, "乙"), (2, "丙"), (2, "丁"),
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
