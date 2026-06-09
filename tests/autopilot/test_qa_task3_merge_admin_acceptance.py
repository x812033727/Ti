"""QA 驗收：任務 #3 「merge 預設不帶 --admin，讓 GitHub 分支保護生效」。

精確釘死 gh pr merge argv 與 --admin gate 雙向行為：
- 預設（MERGE_ADMIN=False）：merge argv 不含 --admin，且含 --squash --delete-branch。
- MERGE_ADMIN=True：merge argv 恰含一個 --admin。
- --admin 由 config.AUTOPILOT_MERGE_ADMIN gate，不寫死（source-level 檢查）。
全程攔截 _run，不碰網路。
"""

from __future__ import annotations

import asyncio
import re

import pytest
from _repo import REPO_ROOT

from studio import autopilot, config

_ROOT = REPO_ROOT
_SRC = (_ROOT / "studio" / "autopilot.py").read_text(encoding="utf-8")
_TASK = {"id": "3", "title": "t", "detail": "d"}
_BRANCH = "autopilot/task-3"


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
            if "merge" in c:
                return c
        return []


@pytest.fixture(autouse=True)
def _no_subprocess(monkeypatch):
    async def _boom(*a, **k):
        raise RuntimeError("禁止真實子行程 / 網路")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _boom)


@pytest.fixture(autouse=True)
def _base_cfg(monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_DRYRUN", False)
    monkeypatch.setattr(config, "AUTOPILOT_FORCE_PUSH", False)
    monkeypatch.setattr(config, "AUTOPILOT_MERGE_ADMIN", False)
    monkeypatch.setattr(config, "AUTOPILOT_REPO", "owner/repo")


def _spy(monkeypatch, overrides=None):
    base = {"rev-list": (0, "1")}  # 有變更；ls-remote 預設 (0,"") 放行
    base.update(overrides or {})
    spy = RunSpy(base)
    monkeypatch.setattr(autopilot, "_run", spy)
    return spy


# ---- 預設不帶 --admin ------------------------------------------------------


def test_default_merge_has_no_admin(monkeypatch):
    spy = _spy(monkeypatch)
    ok, msg = asyncio.run(autopilot._commit_push_merge("/clone", _TASK))
    assert ok, msg
    argv = spy.merge_argv
    assert argv, "未發出 merge"
    assert "--admin" not in argv, "預設不得帶 --admin（須讓分支保護生效）"
    assert "--squash" in argv
    assert "--delete-branch" in argv


# ---- MERGE_ADMIN=1 才帶 --admin -------------------------------------------


def test_merge_admin_flag_adds_admin(monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_MERGE_ADMIN", True)
    spy = _spy(monkeypatch)
    ok, msg = asyncio.run(autopilot._commit_push_merge("/clone", _TASK))
    assert ok, msg
    argv = spy.merge_argv
    assert argv.count("--admin") == 1, "MERGE_ADMIN=1 應恰帶一個 --admin"


# ---- source-level：--admin 由 config gate，非寫死 -------------------------


def test_admin_flag_is_gated_by_config():
    assert "config.AUTOPILOT_MERGE_ADMIN" in _SRC
    # --admin 只能出現在條件式裡（admin_flag），不可無條件寫死於 merge 指令
    assert re.search(r'\["--admin"\]\s+if\s+config\.AUTOPILOT_MERGE_ADMIN', _SRC), (
        "--admin 必須由 config.AUTOPILOT_MERGE_ADMIN 條件控制"
    )


# ---- merge 失敗時回報失敗 --------------------------------------------------


def test_merge_failure_reported(monkeypatch):
    _spy(monkeypatch, {"pr merge": (1, "protected branch hook failed")})
    ok, msg = asyncio.run(autopilot._commit_push_merge("/clone", _TASK))
    assert not ok
    assert "merge 失敗" in msg
