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
        self.prompts: list[str] = []

    async def speak(self, prompt: str, broadcast) -> str:
        self.prompts.append(prompt)
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
    assert out == {
        "role": "engineer",
        "instruction": "把登入頁寫完",
        "end": False,
        "recruit": None,
        "provider": "",
    }


def test_parse_next_step_end_tokens():
    for tok in ("結束", "完成", "END", "done", "stop"):
        assert flow.parse_next_step(f"下一步: {tok}")["end"] is True
        assert flow.parse_next_step(f"下一步: {tok}")["role"] == ""


def test_parse_next_step_fullwidth_colon_and_last_wins():
    out = flow.parse_next_step("下一步：qa\n下一步: senior\n指示: 複審")
    assert out["role"] == "senior" and out["instruction"] == "複審"


def test_parse_next_step_missing_returns_empty():
    assert flow.parse_next_step("一些無關文字") == {
        "role": "",
        "instruction": "",
        "end": False,
        "recruit": None,
        "provider": "",
    }


def test_parse_next_step_extra_tokens_take_first():
    # 多 token（如 `engineer (主寫)`）取第一個——validate 兜底會擋下非法者。
    assert flow.parse_next_step("下一步: engineer 主寫")["role"] == "engineer"


def test_parse_next_step_recruit_and_provider():
    out = flow.parse_next_step(
        "下一步: sec_auditor\n指示: 查授權\n招募: sec_auditor | 資安稽核 | OAuth/JWT 漏洞\nprovider: Codex"
    )
    assert out["role"] == "sec_auditor" and out["instruction"] == "查授權"
    assert out["recruit"] == {
        "key": "sec_auditor",
        "name": "資安稽核",
        "expertise": "OAuth/JWT 漏洞",
    }
    assert out["provider"] == "codex"  # 大小寫正規化
    # 全形管線也接受
    assert flow.parse_next_step("招募: x｜甲｜乙")["recruit"]["key"] == "x"
    # 缺 key 的招募行忽略
    assert flow.parse_next_step("招募:  | 名 | 專長")["recruit"] is None


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


# --- 額度感知分派 (PR-B) ----------------------------------------------------


def _stub_snapshot():
    return {
        "ok": True,
        "updated_at": 1000.0,
        "providers": [
            {
                "key": "claude",
                "ready": True,
                "rate_limits": {
                    "five_hour": {"used_percentage": 30, "reset_at": None},
                    "error": None,
                },
            },
            {
                "key": "codex",
                "ready": True,
                "rate_limits": {
                    "five_hour": {"used_percentage": 95, "reset_at": None},
                    "error": None,
                },
            },
            {"key": "minimax", "ready": False, "rate_limits": None},
            {"key": "antigravity", "ready": False, "rate_limits": None},
        ],
    }


@pytest.mark.asyncio
async def test_dynamic_prompt_includes_quota_summary(monkeypatch):
    from studio import provider_quota

    monkeypatch.setattr(provider_quota, "snapshot", _stub_snapshot)
    s, experts, _ = _session(["下一步: 結束"])
    await s._stage_dynamic({"type": "dynamic", "budget": 3})
    # PM 第一次發言的 prompt 應含額度摘要（claude 用量 30%、codex ⚠️95%）。
    pm_prompt = experts["pm"].prompts[0]
    assert "目前額度" in pm_prompt and "用量 30%" in pm_prompt and "⚠️用量 95%" in pm_prompt


@pytest.mark.asyncio
async def test_refresh_quota_snapshot_failure_is_swallowed(monkeypatch):
    from studio import provider_quota

    def boom():
        raise RuntimeError("network down")

    monkeypatch.setattr(provider_quota, "snapshot", boom)
    s, experts, _ = _session(["下一步: 結束"])
    await s._stage_dynamic({"type": "dynamic", "budget": 3})  # 不應拋
    assert s._quota_snap is None


# --- PM 動態招募 (PR-C) -----------------------------------------------------


def _recording_factory(store):
    def factory(role, cwd, provider):
        e = StubExpert(role, ["新成員已處理"])
        store[role.key] = {"expert": e, "provider": provider}
        return e

    return factory


@pytest.mark.asyncio
async def test_dynamic_recruits_library_role(monkeypatch):
    from studio import provider_quota

    monkeypatch.setattr(provider_quota, "snapshot", _stub_snapshot)
    s, experts, bucket = _session(["下一步: architect\n指示: 複核架構", "下一步: 結束"])
    assert "architect" not in experts  # 初始不在場、但在 BY_KEY（內建可選）
    recruited: dict = {}
    s._recruit_factory = _recording_factory(recruited)
    await s._stage_dynamic({"type": "dynamic", "budget": 5})
    assert "architect" in s._main_ctx.experts  # 已招募加入 lane
    assert recruited["architect"]["expert"].calls >= 1  # 招募的 architect 有發言
    assert s._recruited == 1
    assert any(e.type is events.EventType.EXPERT_JOINED for e in bucket)  # 廣播 EXPERT_JOINED


@pytest.mark.asyncio
async def test_dynamic_recruits_liquid_persona(monkeypatch):
    from studio import provider_quota

    monkeypatch.setattr(provider_quota, "snapshot", _stub_snapshot)
    s, experts, _ = _session(
        [
            "招募: sec_auditor | 資安稽核 | OAuth/JWT 漏洞\n下一步: sec_auditor\n指示: 查授權",
            "下一步: 結束",
        ]
    )
    recruited: dict = {}
    s._recruit_factory = _recording_factory(recruited)
    await s._stage_dynamic({"type": "dynamic", "budget": 5})
    assert "sec_auditor" in s._main_ctx.experts  # 液生 persona 已招募
    assert recruited["sec_auditor"]["expert"].calls >= 1


@pytest.mark.asyncio
async def test_dynamic_recruit_respects_provider_and_rebind(monkeypatch):
    from studio import provider_quota

    monkeypatch.setattr(provider_quota, "snapshot", _stub_snapshot)
    s, experts, _ = _session(["下一步: architect\n指示: 看\nprovider: codex", "下一步: 結束"])
    recruited: dict = {}
    s._recruit_factory = _recording_factory(recruited)
    await s._stage_dynamic({"type": "dynamic", "budget": 5})
    # PM 指定 codex，但 codex 用量 95% 受限 → 自動重綁到最寬鬆就緒者 claude（30%）。
    assert recruited["architect"]["provider"] == "claude"


@pytest.mark.asyncio
async def test_dynamic_recruit_cap(monkeypatch):
    from studio import provider_quota

    monkeypatch.setattr(provider_quota, "snapshot", _stub_snapshot)
    monkeypatch.setattr(config, "RECRUIT_MAX", 0)  # 不准招募
    s, experts, _ = _session(["下一步: architect\n指示: 複核", "下一步: 結束"])
    recruited: dict = {}
    s._recruit_factory = _recording_factory(recruited)
    await s._stage_dynamic({"type": "dynamic", "budget": 5})
    assert "architect" not in s._main_ctx.experts  # 達上限→不招募
    assert recruited == {}
    # fallback：engineer（在場）被指派發言
    assert experts["engineer"].calls >= 1
