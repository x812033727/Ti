"""QA 驗收：任務 #2 「push 前用 ls-remote 三態判定」。

三態（rc 與輸出必須分開判斷）：
 A. rc!=0          → 檢查失敗，中止，絕不 fall-through 當「不存在」、絕不 push。
 B. rc==0 且有輸出 → 遠端已存在同名分支，預設中止（避免覆寫）；FORCE_PUSH=1 才放行。
 C. rc==0 且空輸出 → 遠端不存在，放行 push。

關鍵反例：rc!=0 但輸出為空（失敗卻無訊息）仍須中止——嚴禁把「空輸出」與
「指令失敗」併入同一條件。全程攔截 _run，不碰網路。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from studio import autopilot, config

_ROOT = Path(__file__).resolve().parent.parent
_TASK = {"id": "2", "title": "t", "detail": "d"}
_BRANCH = "autopilot/task-2"


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
    def pushed(self):
        return any("push" in c for c in self.calls)

    @property
    def lsremote_called(self):
        return any("ls-remote" in c for c in self.calls)


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
    base = {"rev-list": (0, "2")}  # 有變更，流程會進到 ls-remote
    base.update(overrides or {})
    spy = RunSpy(base)
    monkeypatch.setattr(autopilot, "_run", spy)
    return spy


# ---- 態 A：rc!=0 → 中止 ----------------------------------------------------


def test_state_a_rc_nonzero_with_output_aborts(monkeypatch):
    spy = _spy(monkeypatch, {"ls-remote": (128, "fatal: could not read from remote")})
    ok, msg = asyncio.run(autopilot._commit_push_merge("/clone", _TASK))
    assert not ok
    assert "ls-remote" in msg and "中止" in msg
    assert spy.lsremote_called
    assert not spy.pushed, "rc!=0 時不得 push"


def test_state_a_rc_nonzero_empty_output_still_aborts(monkeypatch):
    # 關鍵反例：失敗但無輸出，不可被當成「態 C 不存在」放行
    spy = _spy(monkeypatch, {"ls-remote": (1, "")})
    ok, msg = asyncio.run(autopilot._commit_push_merge("/clone", _TASK))
    assert not ok, "rc!=0 即使空輸出也必須中止"
    assert not spy.pushed


# ---- 態 B：rc==0 且有輸出 → 預設中止；FORCE_PUSH 才放行 --------------------


def test_state_b_exists_default_aborts(monkeypatch):
    spy = _spy(monkeypatch, {"ls-remote": (0, "deadbeef\trefs/heads/" + _BRANCH)})
    ok, msg = asyncio.run(autopilot._commit_push_merge("/clone", _TASK))
    assert not ok
    assert "已存在" in msg or "已中止" in msg
    assert not spy.pushed, "遠端已存在分支時預設不得覆寫"


def test_state_b_exists_with_force_push_proceeds(monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_FORCE_PUSH", True)
    spy = _spy(monkeypatch, {"ls-remote": (0, "deadbeef\trefs/heads/" + _BRANCH)})
    ok, msg = asyncio.run(autopilot._commit_push_merge("/clone", _TASK))
    assert ok, msg
    assert spy.pushed, "FORCE_PUSH=1 應放行覆寫"


# ---- 態 C：rc==0 且空輸出 → 放行 ------------------------------------------


def test_state_c_absent_proceeds(monkeypatch):
    spy = _spy(monkeypatch, {"ls-remote": (0, "")})
    ok, msg = asyncio.run(autopilot._commit_push_merge("/clone", _TASK))
    assert ok, msg
    assert spy.pushed, "遠端不存在應放行 push"


def test_state_c_whitespace_only_output_treated_as_absent(monkeypatch):
    # 僅空白也應視為「不存在」（程式用 out.strip()）
    spy = _spy(monkeypatch, {"ls-remote": (0, "   \n  ")})
    ok, msg = asyncio.run(autopilot._commit_push_merge("/clone", _TASK))
    assert ok, msg
    assert spy.pushed


# ---- 三態互斥：rc 與輸出分開判斷（不可併入同一條件） ----------------------


def test_rc_checked_before_output(monkeypatch):
    # rc!=0 且有「看似分支」輸出時，仍走「檢查失敗」分支而非「已存在」分支
    spy = _spy(monkeypatch, {"ls-remote": (2, "deadbeef\trefs/heads/" + _BRANCH)})
    ok, msg = asyncio.run(autopilot._commit_push_merge("/clone", _TASK))
    assert not ok
    assert "ls-remote 檢查失敗" in msg, "rc!=0 必須優先於輸出判定"
    assert not spy.pushed
