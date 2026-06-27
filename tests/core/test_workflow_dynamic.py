"""動態 step（workflow dynamic stage）的執行語意與防呆離線測試。

涵蓋：flow.parse_next_step 解析、_stage_dynamic 的 budget 硬上限、`下一步: 結束` 提早收斂、
非法角色 fallback、停滯（is_stalled）收斂、被中止（_stop）立即結束。全程 stub 專家、不呼叫 LLM。
"""

from __future__ import annotations

import pytest

from studio import config, events, flow
from studio.orchestrator import LaneContext, StudioSession
from studio.roles import BY_KEY, Role


class StubExpert:
    """依序回傳腳本化回應，記錄被呼叫次數。"""

    def __init__(self, role: Role, scripts: list[str]):
        self.role = role
        self._scripts = scripts
        self.calls = 0

    async def speak(self, prompt: str, broadcast) -> str:
        text = self._scripts[min(self.calls, len(self._scripts) - 1)]
        self.calls += 1
        await broadcast(
            events.expert_message("t", self.role.key, self.role.name, self.role.avatar, text)
        )
        return text

    async def stop(self) -> None:
        pass


def _session(pm_scripts):
    bucket: list[events.StudioEvent] = []

    async def broadcast(ev):
        bucket.append(ev)

    experts = {
        "pm": StubExpert(BY_KEY["pm"], pm_scripts),
        "engineer": StubExpert(BY_KEY["engineer"], ["工程師已處理"]),
        "qa": StubExpert(BY_KEY["qa"], ["QA 已驗證"]),
        "senior": StubExpert(BY_KEY["senior"], ["高工已審查"]),
    }
    s = StudioSession("t", broadcast, experts=experts, cwd=None)
    s._main_ctx = LaneContext("main", None, experts, None)
    s._requirement = "做一個小工具"
    return s, experts, bucket


# --- flow.parse_next_step ----------------------------------------------------


def test_parse_next_step_basic():
    out = flow.parse_next_step("下一步: engineer\n指示: 把登入頁寫完")
    assert out == {"role": "engineer", "instruction": "把登入頁寫完", "end": False}


def test_parse_next_step_end_tokens():
    for tok in ("結束", "完成", "END", "done", "stop"):
        assert flow.parse_next_step(f"下一步: {tok}")["end"] is True
        assert flow.parse_next_step(f"下一步: {tok}")["role"] == ""


def test_parse_next_step_fullwidth_colon_and_last_wins():
    out = flow.parse_next_step("下一步：qa\n下一步: senior\n指示: 複審")
    assert out["role"] == "senior" and out["instruction"] == "複審"


def test_parse_next_step_missing_returns_empty():
    assert flow.parse_next_step("一些無關文字") == {"role": "", "instruction": "", "end": False}


def test_parse_next_step_extra_tokens_take_first():
    # 多 token（如 `engineer (主寫)`）取第一個——validate 兜底會擋下非法者。
    assert flow.parse_next_step("下一步: engineer 主寫")["role"] == "engineer"


# --- _stage_dynamic 防呆 ------------------------------------------------------


@pytest.mark.asyncio
async def test_dynamic_budget_caps_hops():
    # PM 每步都派 engineer（給不同指示避免 stall），budget=2 → 恰 2 個 hop。
    s, experts, _ = _session(
        [
            "下一步: engineer\n指示: A",
            "下一步: engineer\n指示: B",
            "下一步: engineer\n指示: C",
        ]
    )
    await s._stage_dynamic({"type": "dynamic", "budget": 2})
    assert experts["pm"].calls == 2
    assert experts["engineer"].calls == 2


@pytest.mark.asyncio
async def test_dynamic_end_stops_early():
    s, experts, _ = _session(["下一步: engineer\n指示: A", "下一步: 結束"])
    await s._stage_dynamic({"type": "dynamic", "budget": 5})
    # hop0 派 engineer；hop1 PM 說結束 → 不再派人。
    assert experts["pm"].calls == 2
    assert experts["engineer"].calls == 1


@pytest.mark.asyncio
async def test_dynamic_invalid_role_falls_back():
    s, experts, _ = _session(["下一步: ghost_role\n指示: 做點事", "下一步: 結束"])
    await s._stage_dynamic({"type": "dynamic", "budget": 5, "fallback": "engineer"})
    # 非法角色 → fallback engineer 實際發言。
    assert experts["engineer"].calls == 1


@pytest.mark.asyncio
async def test_dynamic_stalls_on_repetition(monkeypatch):
    monkeypatch.setattr(config, "STALL_ROUNDS", 3)
    # PM 連續輸出相同決策 → 第 3 輪 is_stalled 觸發、提早收斂（< budget=5）。
    s, experts, _ = _session(["下一步: engineer\n指示: 同樣的事"])
    await s._stage_dynamic({"type": "dynamic", "budget": 5})
    assert experts["pm"].calls == 3
    assert experts["engineer"].calls == 2  # 停滯那輪在派人前就 break


@pytest.mark.asyncio
async def test_dynamic_stop_breaks_immediately():
    s, experts, _ = _session(["下一步: engineer\n指示: A"])
    s._stop = True
    await s._stage_dynamic({"type": "dynamic", "budget": 5})
    assert experts["pm"].calls == 0  # 一開始就被中止，不發任何言
