"""QA 驗收：任務 #11（總驗收標準 #5）「push 旗標預設安全側，且可由環境變數覆寫」端到端。

不只測 config 值（見 task1），本檔把「config 值」與「實際 push 行為」串起來，
釘死語意：預設 = 非強制推送；環境變數覆寫後 push flag 跟著變。合併本身一律走
publisher._merge_flow（等 CI→綠才合併），本檔將其 monkeypatch 成 MERGED 以聚焦 push 旗標。

- 清乾淨環境重載 config：FORCE_PUSH 預設 False，且實際 push 無 force token、合併走 _merge_flow。
- TI_AUTOPILOT_FORCE_PUSH=1：實際 push 改用 --force-with-lease --force-if-includes。
- 環境變數解析語意：("0","false","False","",未設) → 安全側 False；其餘 → True。

全程攔截 autopilot._run 與 publisher._merge_flow，不碰網路。
"""

from __future__ import annotations

import asyncio
import importlib
import os

import pytest

from studio import autopilot, config, publisher

_TASK = {"id": "11", "title": "t", "detail": ""}
_BRANCH = "autopilot/task-11"
_ENVS = ("TI_AUTOPILOT_FORCE_PUSH",)


class RunSpy:
    def __init__(self, overrides=None):
        self.overrides = overrides or {}
        self.calls: list[list[str]] = []

    async def __call__(self, cmd, cwd=None, timeout=600, **kwargs):
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
    def merge_flow_called(self):
        return self._merge_flow_called


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
    """裝上 _run spy，並把合併協調器 monkeypatch 成「直接 MERGED」。

    回傳的 spy 另記錄 _merge_flow 是否被呼叫，用以斷言「走的是等 CI 的協調器、
    而非裸 gh pr merge」。`gh pr view --json number` 需回一個數字，否則
    _commit_push_merge 會在取 PR 編號處提早失敗。
    """
    branch = config.AUTOPILOT_BRANCH
    repo = config.AUTOPILOT_REPO
    # 新增的 branch protection 防線在呼叫 _commit_push_merge 時會打兩條 API；在 E2E 單元中提供
    # 穩定 mock，避免 test 受未模擬 GitHub API 輸出影響而誤判。
    protection_overrides = {
        f"repos/{repo}/rules/branches/{branch}": (0, "[]"),
        f"repos/{repo}/branches/{branch}/protection": (1, "HTTP 404"),
        "git remote get-url --push origin": (0, f"https://github.com/{repo}.git"),
        "pr view": (0, "123"),
    }
    spy = RunSpy({**overrides, **protection_overrides})
    spy._merge_flow_called = False
    monkeypatch.setattr(autopilot, "_run", spy)

    async def _fake_merge_flow(*args, **kwargs):
        spy._merge_flow_called = True
        return publisher.MergeOutcome.MERGED, "deadbeef"

    monkeypatch.setattr(publisher, "_merge_flow", _fake_merge_flow)

    # 隔離：本檔聚焦「push 旗標安全」；合併目標的保護狀態檢查是各自獨立的第二道防線、
    # 另有專測（tests/autopilot/test_qa_task2_protection_merge_gate）。此處 stub 成「明確無保護」
    # 放行，避免 RunSpy 未模擬保護 API 而回 unknown 觸發 fail-safe 中止、淹沒本檔要驗的 push 旗標。
    async def _fake_protection(*args, **kwargs):
        return "unprotected", ""

    monkeypatch.setattr(autopilot, "_check_branch_protection", _fake_protection)
    return spy


_HAS_CHANGE = {"rev-list --count": (0, "1")}
_REMOTE_EXISTS = {"ls-remote --heads": (0, f"x\trefs/heads/{_BRANCH}\n")}


# === 預設（無 env）→ config 安全側 + 實際行為安全側 ===================


@pytest.mark.asyncio
async def test_clean_defaults_behave_safe(monkeypatch):
    _reload_with(monkeypatch, {})  # 完全不設 env
    assert config.AUTOPILOT_FORCE_PUSH is False

    spy = _install(monkeypatch, {**_HAS_CHANGE})  # 遠端不存在
    ok, _ = await autopilot._commit_push_merge("/clone", _TASK)
    assert ok is True
    # 非強制推送
    for bad in ("-f", "--force", "--force-with-lease", "--force-if-includes"):
        assert bad not in spy.push_argv
    # 走 publisher._merge_flow（等 CI→合併），而非裸 gh pr merge
    assert spy.merge_flow_called is True
    assert next((c for c in spy.calls if "merge" in c and "pr" in c), []) == []


# === env 覆寫 FORCE_PUSH=1 → push 行為變強制 ==========================


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
    assert spy.merge_flow_called is True


# === 解析語意：安全側值 vs 啟用值 ====================================


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
