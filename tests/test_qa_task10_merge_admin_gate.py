"""QA 驗收：任務 #10（總驗收標準 #4）「gh pr merge 預設不帶 --admin，僅旗標真才加」。

精確釘死 merge argv 與旗標 gate 雙向，並端到端驗證環境變數驅動：
- MERGE_ADMIN=False：merge argv 精確為 gh pr merge -R <repo> <branch> --squash --delete-branch。
- MERGE_ADMIN=True：精確為 ... --squash --admin --delete-branch（--admin 在 squash 後、delete 前）。
- 端到端：TI_AUTOPILOT_MERGE_ADMIN 環境變數 reload config 後直接影響 argv。
- source-level：--admin 由 config.AUTOPILOT_MERGE_ADMIN gate，不寫死。

全程攔截 autopilot._run，不碰網路。
"""

from __future__ import annotations

import asyncio
import importlib
import os
from pathlib import Path

import pytest

from studio import autopilot, config

_GH = ["gh"]
_ROOT = Path(__file__).resolve().parent.parent
_TASK = {"id": "10", "title": "t", "detail": ""}
_BRANCH = "autopilot/task-10"


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
    def merge_argv(self):
        for c in self.calls:
            if "merge" in c and "pr" in c:
                return c
        return []


@pytest.fixture(autouse=True)
def _forbid_real_subprocess(monkeypatch):
    async def _boom(*args, **kwargs):
        raise RuntimeError("test forbids spawning a real subprocess (no network)")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _boom)


@pytest.fixture(autouse=True)
def _base_config(monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_DRYRUN", False)
    monkeypatch.setattr(config, "AUTOPILOT_FORCE_PUSH", False)
    monkeypatch.setattr(config, "AUTOPILOT_BRANCH", "main")


def _install(monkeypatch, overrides):
    spy = RunSpy(overrides)
    monkeypatch.setattr(autopilot, "_run", spy)
    return spy


_HAS_CHANGE = {"rev-list --count": (0, "1")}


# === 精確 argv：預設無 --admin ========================================


@pytest.mark.asyncio
async def test_default_merge_exact_argv(monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_MERGE_ADMIN", False)
    spy = _install(monkeypatch, {**_HAS_CHANGE})
    ok, _ = await autopilot._commit_push_merge("/clone", _TASK)
    assert ok is True
    repo = config.AUTOPILOT_REPO
    assert spy.merge_argv == [
        *_GH, "pr", "merge", "-R", repo, _BRANCH, "--squash", "--delete-branch",
    ]
    assert "--admin" not in spy.merge_argv


# === 精確 argv：旗標開啟才有 --admin，且位置正確 =====================


@pytest.mark.asyncio
async def test_admin_on_exact_argv(monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_MERGE_ADMIN", True)
    spy = _install(monkeypatch, {**_HAS_CHANGE})
    ok, _ = await autopilot._commit_push_merge("/clone", _TASK)
    assert ok is True
    repo = config.AUTOPILOT_REPO
    assert spy.merge_argv == [
        *_GH, "pr", "merge", "-R", repo, _BRANCH, "--squash", "--admin", "--delete-branch",
    ]
    # --admin 緊接在 --squash 之後、--delete-branch 之前
    argv = spy.merge_argv
    assert argv[argv.index("--squash") + 1] == "--admin"
    assert argv[argv.index("--admin") + 1] == "--delete-branch"


# === 端到端：環境變數驅動 =============================================


@pytest.mark.asyncio
@pytest.mark.parametrize("env_val,expect_admin", [("0", False), ("", False), ("1", True), ("true", True)])
async def test_env_drives_admin_flag(monkeypatch, env_val, expect_admin):
    """TI_AUTOPILOT_MERGE_ADMIN 由環境變數 reload config 後，直接決定 argv 是否含 --admin。"""
    monkeypatch.setenv("TI_AUTOPILOT_MERGE_ADMIN", env_val)
    importlib.reload(config)
    # reload 後重設本測試所需的其他 config（_base_config 的 monkeypatch 已被 reload 覆蓋）
    monkeypatch.setattr(config, "AUTOPILOT_DRYRUN", False)
    monkeypatch.setattr(config, "AUTOPILOT_FORCE_PUSH", False)
    monkeypatch.setattr(config, "AUTOPILOT_BRANCH", "main")
    try:
        spy = _install(monkeypatch, {**_HAS_CHANGE})
        await autopilot._commit_push_merge("/clone", _TASK)
        assert ("--admin" in spy.merge_argv) is expect_admin
    finally:
        monkeypatch.delenv("TI_AUTOPILOT_MERGE_ADMIN", raising=False)
        importlib.reload(config)


# === source-level：--admin 由 config gate，不寫死 =====================


def test_admin_gated_by_config_in_source():
    text = (_ROOT / "studio" / "autopilot.py").read_text(encoding="utf-8")
    assert "config.AUTOPILOT_MERGE_ADMIN" in text
    # 不得寫死 --squash --admin 連續字面
    assert '"--squash", "--admin"' not in text
    assert "'--squash', '--admin'" not in text
