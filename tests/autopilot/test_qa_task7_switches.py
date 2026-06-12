"""QA 驗收：任務 #7「可關閉」驗收標準專測。

驗收標準：
- 新機制都有 config 開關（HUDDLE_ENABLED / CRITIC_ENABLED / NOTES_ENABLED / STALL_ROUNDS）。
- 預設行為對既有測試保持相容：預設一律不啟用（huddle/critic/notes 預設 False、
  stall 在無 cwd/關 git 時不誤觸），全部關閉時流程等同 legacy 線性管線。
"""

from __future__ import annotations

import importlib

import pytest

from studio import config, events, workspace
from studio.orchestrator import StudioSession
from studio.roles import BY_KEY, Role


class StubExpert:
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


def collect():
    bucket: list[events.StudioEvent] = []

    async def broadcast(ev: events.StudioEvent) -> None:
        bucket.append(ev)

    return bucket, broadcast


@pytest.fixture(autouse=True)
def _no_debate(monkeypatch):
    monkeypatch.setattr(config, "DEBATE_ROUNDS", 0)


def _experts(pm, eng, qa, senior):
    return {
        "pm": StubExpert(BY_KEY["pm"], pm),
        "engineer": StubExpert(BY_KEY["engineer"], eng),
        "qa": StubExpert(BY_KEY["qa"], qa),
        "senior": StubExpert(BY_KEY["senior"], senior),
    }


# === 開關存在且型別正確 ================================================


def test_all_switches_exist():
    assert isinstance(config.HUDDLE_ENABLED, bool)
    assert isinstance(config.CRITIC_ENABLED, bool)
    assert isinstance(config.NOTES_ENABLED, bool)
    assert isinstance(config.STALL_ROUNDS, int)


# === 環境變數解析：明確設值可開可關；未設走各自預設 =====================


@pytest.mark.parametrize(
    "val,expected",
    [
        ("0", False),
        ("false", False),
        ("False", False),
        ("", False),
        ("1", True),
        ("true", True),
        ("yes", True),  # 任何非關閉值 → 啟用
    ],
)
def test_flag_env_parsing(monkeypatch, val, expected):
    """三個布林開關的環境變數解析一致（明確設值時）。"""
    for env in ("TI_HUDDLE", "TI_CRITIC", "TI_NOTES"):
        monkeypatch.setenv(env, val)
    try:
        importlib.reload(config)
        assert config.HUDDLE_ENABLED is expected
        assert config.CRITIC_ENABLED is expected
        assert config.NOTES_ENABLED is expected
    finally:
        # 還原成測試環境的預設，避免污染後續測試
        for env in ("TI_HUDDLE", "TI_CRITIC", "TI_NOTES"):
            monkeypatch.delenv(env, raising=False)
        importlib.reload(config)


def test_stall_rounds_env_parsing(monkeypatch):
    monkeypatch.setenv("TI_STALL_ROUNDS", "5")
    try:
        importlib.reload(config)
        assert config.STALL_ROUNDS == 5
    finally:
        monkeypatch.delenv("TI_STALL_ROUNDS", raising=False)
        importlib.reload(config)
    # 預設值
    assert config.STALL_ROUNDS == 3


def test_defaults():
    """重載乾淨環境後的預設：huddle／notes 開（學習機制預設啟用）、critic 關（opt-in）。"""
    import os

    for env in ("TI_HUDDLE", "TI_CRITIC", "TI_NOTES"):
        os.environ.pop(env, None)
    importlib.reload(config)
    assert config.HUDDLE_ENABLED is True
    assert config.CRITIC_ENABLED is False
    assert config.NOTES_ENABLED is True


# === 全部關閉 → 行為等同 legacy 線性管線 ==============================


@pytest.mark.asyncio
async def test_all_off_behaves_like_legacy_on_success(monkeypatch, tmp_path):
    """成功路徑：全關時無任何新事件、無 NOTES.md，照常完成。"""
    monkeypatch.setattr(config, "HUDDLE_ENABLED", False)
    monkeypatch.setattr(config, "CRITIC_ENABLED", False)
    monkeypatch.setattr(config, "NOTES_ENABLED", False)
    monkeypatch.setattr(config, "OFFLINE_MODE", False)  # 確保不自動啟用 critic
    monkeypatch.setattr(config, "ENABLE_GIT", False)
    monkeypatch.setattr(config, "WORKSPACE_ROOT", tmp_path)
    sid = "legacy_ok"
    workspace.create_workspace(sid)

    bucket, broadcast = collect()
    experts = _experts(
        pm=["任務: 實作", "決議: 完成", "檢討"],
        eng=["做好了"],
        qa=["驗證: PASS"],
        senior=["決議: 核可"],
    )
    session = StudioSession(sid, broadcast, experts=experts, cwd=workspace.workspace_path(sid))
    await session.run("需求")

    ts = [e.type for e in bucket]
    assert events.EventType.HUDDLE not in ts
    assert events.EventType.CRITIC_REVIEW not in ts
    assert workspace.read_notes(sid) == ""
    assert not (workspace.workspace_path(sid) / "NOTES.md").exists()
    done = [e for e in bucket if e.type == events.EventType.DONE][0]
    assert done.payload["completed"] is True


@pytest.mark.asyncio
async def test_all_off_behaves_like_legacy_on_persistent_failure(monkeypatch):
    """失敗路徑：全關時滿輪即停（無 huddle/重試），任務靜默標 review（legacy 行為）。"""
    monkeypatch.setattr(config, "HUDDLE_ENABLED", False)
    monkeypatch.setattr(config, "CRITIC_ENABLED", False)
    monkeypatch.setattr(config, "NOTES_ENABLED", False)
    monkeypatch.setattr(config, "OFFLINE_MODE", False)
    bucket, broadcast = collect()
    experts = _experts(
        pm=["任務: 實作", "決議: 未完成", "檢討"],
        eng=["一版"],
        qa=["驗證: FAIL"],
        senior=["決議: 退回"],
    )
    session = StudioSession("t", broadcast, experts=experts, cwd=None)
    await session.run("需求")

    # 跑滿 TASK_MAX_ROUNDS 即停，無 huddle、無重試
    assert experts["engineer"].calls == config.TASK_MAX_ROUNDS
    ts = [e.type for e in bucket]
    assert events.EventType.HUDDLE not in ts
    assert events.EventType.CRITIC_REVIEW not in ts
    # 任務維持 review、未標已知限制（legacy）
    assert session._tasks[0]["status"] == "review"
    assert session._tasks[0].get("limitation") is not True


@pytest.mark.asyncio
async def test_stall_disabled_via_rounds_le_one(monkeypatch, tmp_path):
    """STALL_ROUNDS<=1 視為停用停滯守門：即使重述也跑滿輪數。"""
    from studio import runner

    async def _noop_init(cwd):
        return True

    async def _noop_commit(cwd, message):
        return None

    monkeypatch.setattr(runner, "git_init", _noop_init)
    monkeypatch.setattr(runner, "git_commit", _noop_commit)
    monkeypatch.setattr(config, "ENABLE_GIT", True)
    monkeypatch.setattr(config, "STALL_ROUNDS", 1)  # 停用
    monkeypatch.setattr(config, "TASK_MAX_ROUNDS", 3)
    monkeypatch.setattr(config, "WORKSPACE_ROOT", tmp_path)
    # 本測試只驗停滯守門：pin 掉會多加輪次/呼叫的機制（其預設已開）。
    monkeypatch.setattr(config, "HUDDLE_ENABLED", False)
    monkeypatch.setattr(config, "REFLEXION_ENABLED", False)
    sid = "stalloff"
    workspace.create_workspace(sid)

    bucket, broadcast = collect()
    experts = _experts(
        pm=["任務: 實作", "決議: 未完成", "檢討"],
        eng=["完全相同的重述發言"],
        qa=["驗證: FAIL"],
        senior=["決議: 退回"],
    )
    session = StudioSession(sid, broadcast, experts=experts, cwd=workspace.workspace_path(sid))
    await session.run("需求")

    assert experts["engineer"].calls == 3  # 未提早收斂
    phases = [e.payload["phase"] for e in bucket if e.type == events.EventType.PHASE_CHANGE]
    assert "停滯收斂" not in phases
