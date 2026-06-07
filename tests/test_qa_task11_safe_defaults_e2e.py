"""QA 驗收：任務 #11（總驗收標準 #5）「旗標預設安全側，且可由環境變數覆寫」端到端。

不只測 config 值（見 task1），本檔把「config 值」與「實際 push/merge 行為」串起來，
釘死語意：預設 = 非強制 + 不繞過保護；環境變數覆寫後行為跟著變。

- 清乾淨環境重載 config：兩旗標預設 False，且實際 push 無 force token、merge 無 --admin。
- TI_AUTOPILOT_FORCE_PUSH=1：實際 push 改用 --force-with-lease --force-if-includes。
- TI_AUTOPILOT_MERGE_ADMIN=1：實際 merge 帶 --admin。
- 環境變數解析語意：("0","false","False","",未設) → 安全側 False；其餘 → True。

全程攔截 autopilot._run，不碰網路。
"""

from __future__ import annotations

import asyncio
import importlib
import os

import pytest

from studio import autopilot, config

_TASK = {"id": "11", "title": "t", "detail": ""}
_BRANCH = "autopilot/task-11"
_ENVS = ("TI_AUTOPILOT_FORCE_PUSH", "TI_AUTOPILOT_MERGE_ADMIN")


class RunSpy:
    def __init__(self, overrides=None):
        self.overrides = overrides or {}
        self.calls: list[list[str]] = []

    async def __call__(self, cmd, cwd=None, timeout=600):
        self.calls.append(list(cmd))
        joined = " ".join(cmd)
        for key, val in self.overrides.items():
            if key in joined:
                return val
        return (0, "")

    @property
    def push_argv(self):
        return next((c for c in self.calls if "push" in c), [])

    @property
    def merge_argv(self):
        return next((c for c in self.calls if "merge" in c and "pr" in c), [])


@pytest.fixture(autouse=True)
def _forbid_real_subprocess(monkeypatch):
    async def _boom(*args, **kwargs):
        raise RuntimeError("test forbids spawning a real subprocess (no network)")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _boom)


@pytest.fixture(autouse=True)
def _restore_config():
    """每測試後清環境並重載 config，避免污染。"""
    yield
    for env in _ENVS:
        os.environ.pop(env, None)
    importlib.reload(config)


def _reload_with(monkeypatch, env_map):
    for env in _ENVS:
        monkeypatch.delenv(env, raising=False)
    for k, v in env_map.items():
        monkeypatch.setenv(k, v)
    importlib.reload(config)
    # reload 後補上行為所需的其他 config（非本任務變數）
    monkeypatch.setattr(config, "AUTOPILOT_DRYRUN", False)
    monkeypatch.setattr(config, "AUTOPILOT_BRANCH", "main")


def _install(monkeypatch, overrides):
    spy = RunSpy(overrides)
    monkeypatch.setattr(autopilot, "_run", spy)
    return spy


_HAS_CHANGE = {"rev-list --count": (0, "1")}
_REMOTE_EXISTS = {"ls-remote --heads": (0, f"x\trefs/heads/{_BRANCH}\n")}


# === 預設（無 env）→ config 安全側 + 實際行為安全側 ===================


@pytest.mark.asyncio
async def test_clean_defaults_behave_safe(monkeypatch):
    _reload_with(monkeypatch, {})  # 完全不設兩個 env
    assert config.AUTOPILOT_FORCE_PUSH is False
    assert config.AUTOPILOT_MERGE_ADMIN is False

    spy = _install(monkeypatch, {**_HAS_CHANGE})  # 遠端不存在
    ok, _ = await autopilot._commit_push_merge("/clone", _TASK)
    assert ok is True
    # 非強制推送
    for bad in ("-f", "--force", "--force-with-lease", "--force-if-includes"):
        assert bad not in spy.push_argv
    # 不繞過保護
    assert "--admin" not in spy.merge_argv


# === env 覆寫 FORCE_PUSH=1 → 行為變強制 ===============================


@pytest.mark.asyncio
async def test_env_force_push_overrides_behavior(monkeypatch):
    _reload_with(monkeypatch, {"TI_AUTOPILOT_FORCE_PUSH": "1"})
    assert config.AUTOPILOT_FORCE_PUSH is True
    # 即使遠端已存在也不中止（force gate）
    spy = _install(monkeypatch, {**_HAS_CHANGE, **_REMOTE_EXISTS})
    ok, _ = await autopilot._commit_push_merge("/clone", _TASK)
    assert ok is True
    assert "--force-with-lease" in spy.push_argv
    assert "--force-if-includes" in spy.push_argv
    assert "-f" not in spy.push_argv


# === env 覆寫 MERGE_ADMIN=1 → 行為帶 admin ============================


@pytest.mark.asyncio
async def test_env_merge_admin_overrides_behavior(monkeypatch):
    _reload_with(monkeypatch, {"TI_AUTOPILOT_MERGE_ADMIN": "1"})
    assert config.AUTOPILOT_MERGE_ADMIN is True
    spy = _install(monkeypatch, {**_HAS_CHANGE})
    ok, _ = await autopilot._commit_push_merge("/clone", _TASK)
    assert ok is True
    assert "--admin" in spy.merge_argv


# === 解析語意：安全側值 vs 啟用值（兩旗標一致）======================


@pytest.mark.parametrize(
    "val,expected",
    [
        (None, False),
        ("0", False),
        ("false", False),
        ("False", False),
        ("", False),
        ("1", True),
        ("true", True),
        ("on", True),
    ],
)
def test_env_parsing_safe_side(monkeypatch, val, expected):
    env_map = {} if val is None else {e: val for e in _ENVS}
    _reload_with(monkeypatch, env_map)
    assert config.AUTOPILOT_FORCE_PUSH is expected
    assert config.AUTOPILOT_MERGE_ADMIN is expected
