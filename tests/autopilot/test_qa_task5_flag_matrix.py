"""QA 驗收：任務 #5「補上單元測試——push/merge 安全旗標矩陣」整合專測。

本檔是任務 #5 的明確交付物：以「五情境矩陣」一處集中驗證 _commit_push_merge
在 FORCE_PUSH × 遠端分支狀態 的所有組合下行為正確，含架構決策指定、易被
漏測的死碼交集情境（force 開啟＋遠端已存在 → 不中止且走 force-with-lease）。

對應總驗收標準 #1/#2/#3/#4：
- 預設情境：非強制推送（無 -f/--force/lease）、merge 無 --admin。
- 遠端已存在分支且未開 FORCE_PUSH：中止，回 (False, ...)，不執行任何 push。
- FORCE_PUSH 開啟：push 帶 --force-with-lease --force-if-includes（無裸 -f），
  且遠端已存在時不中止（force 非死碼）。
- ls-remote 失敗（rc!=0）：中止，不 push。
- MERGE_ADMIN 兩態：預設無 --admin、開啟才帶 --admin。

手法：攔截 autopilot._run，依指令分派可控結果並擷取 push / merge argv。
全程不發起真實 git/gh/網路操作。
"""

from __future__ import annotations

import asyncio

import pytest

from studio import autopilot, config

_TASK = {"id": "5", "title": "矩陣驗證任務", "detail": ""}
_BRANCH = "autopilot/task-5"
_REMOTE_EXISTS = f"deadbeef\trefs/heads/{_BRANCH}\n"


class RunSpy:
    """攔截 autopilot._run：依指令關鍵片段回傳 (rc, out)，並記錄整個呼叫序列。"""

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

    def argv_with(self, *needles) -> list[str]:
        for c in self.calls:
            if all(n in c for n in needles):
                return c
        return []

    @property
    def push_argv(self) -> list[str]:
        return self.argv_with("push")

    @property
    def merge_argv(self) -> list[str]:
        return self.argv_with("pr", "merge")

    def called(self, fragment: str) -> bool:
        return any(fragment in " ".join(c) for c in self.calls)


@pytest.fixture(autouse=True)
def _forbid_real_subprocess(monkeypatch):
    async def _boom(*args, **kwargs):
        raise RuntimeError("test forbids spawning a real subprocess (no network)")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _boom)


@pytest.fixture(autouse=True)
def _base_config(monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_DRYRUN", False)
    monkeypatch.setattr(config, "AUTOPILOT_BRANCH", "main")
    monkeypatch.setattr(config, "AUTOPILOT_MERGE_ADMIN", False)


async def _run_case(monkeypatch, *, force_push, remote_exists=False, lsremote_rc=0):
    """以指定旗標/遠端狀態跑一次 _commit_push_merge，回傳 (ok, msg, spy)。"""
    monkeypatch.setattr(config, "AUTOPILOT_FORCE_PUSH", force_push)
    overrides = {"rev-list --count": (0, "1")}  # 恆有變更可合併
    if lsremote_rc != 0:
        overrides["ls-remote --heads"] = (lsremote_rc, "fatal: could not read from remote")
    elif remote_exists:
        overrides["ls-remote --heads"] = (0, _REMOTE_EXISTS)
    spy = RunSpy(overrides)
    monkeypatch.setattr(autopilot, "_run", spy)
    ok, msg = await autopilot._commit_push_merge("/clone", _TASK)
    return ok, msg, spy


def _assert_non_forced(argv):
    assert argv, "未呼叫 push"
    assert "push" in argv and "-u" in argv and "origin" in argv and _BRANCH in argv
    for bad in ("-f", "--force", "--force-with-lease", "--force-if-includes"):
        assert bad not in argv, f"非強制推送不應含 {bad}：{argv}"


def _assert_forced_with_lease(argv):
    assert argv, "未呼叫 push"
    assert "--force-with-lease" in argv and "--force-if-includes" in argv
    assert "-f" not in argv, f"不得使用裸 -f：{argv}"
    assert "--force" not in argv, f"不得使用裸 --force：{argv}"


# === 情境 1：預設（force=F、遠端不存在）→ 非強制推送、merge 無 --admin ==


@pytest.mark.asyncio
async def test_case1_default_non_forced(monkeypatch):
    ok, msg, spy = await _run_case(monkeypatch, force_push=False, remote_exists=False)
    assert ok is True
    _assert_non_forced(spy.push_argv)
    assert "--admin" not in spy.merge_argv
    # ls-remote 確實在 push 前跑過
    assert spy.called(f"ls-remote --heads origin {_BRANCH}")


# === 情境 2：遠端已存在 + force=F → 中止、不 push =====================


@pytest.mark.asyncio
async def test_case2_remote_exists_aborts(monkeypatch):
    ok, msg, spy = await _run_case(monkeypatch, force_push=False, remote_exists=True)
    assert ok is False
    assert not spy.called("push"), f"中止情境不應 push：{spy.calls}"
    assert not spy.called("pr merge")
    assert "遠端已存在" in msg and "TI_AUTOPILOT_FORCE_PUSH=1" in msg


# === 情境 3：force=T、遠端不存在 → 強制推送（lease + if-includes）=====


@pytest.mark.asyncio
async def test_case3_force_remote_absent(monkeypatch):
    ok, msg, spy = await _run_case(monkeypatch, force_push=True, remote_exists=False)
    assert ok is True
    _assert_forced_with_lease(spy.push_argv)


# === 情境 4（死碼交集）：force=T、遠端已存在 → 不中止且強制推送 ======


@pytest.mark.asyncio
async def test_case4_force_remote_exists_not_aborted(monkeypatch):
    """架構決策指定的交集情境：FORCE_PUSH 開啟時略過中止，直接走 force-with-lease，
    杜絕 force 變死碼。"""
    ok, msg, spy = await _run_case(monkeypatch, force_push=True, remote_exists=True)
    assert ok is True, f"force 開啟＋遠端已存在不應中止：{msg}"
    _assert_forced_with_lease(spy.push_argv)


# === 情境 5：ls-remote 失敗（rc!=0）→ 中止、不 push ==================


@pytest.mark.asyncio
async def test_case5_lsremote_failure_aborts(monkeypatch):
    ok, msg, spy = await _run_case(monkeypatch, force_push=False, lsremote_rc=128)
    assert ok is False
    assert not spy.called("push"), f"ls-remote 失敗不應 fall-through 去 push：{spy.calls}"
    assert "ls-remote 檢查失敗" in msg or "無法確認遠端狀態" in msg


# === 補充：MERGE_ADMIN 兩態 × 預設非強制推送 ==========================


@pytest.mark.asyncio
@pytest.mark.parametrize("admin,expect_admin", [(False, False), (True, True)])
async def test_merge_admin_matrix(monkeypatch, admin, expect_admin):
    monkeypatch.setattr(config, "AUTOPILOT_MERGE_ADMIN", admin)
    ok, msg, spy = await _run_case(monkeypatch, force_push=False, remote_exists=False)
    assert ok is True
    assert ("--admin" in spy.merge_argv) is expect_admin
    assert "--squash" in spy.merge_argv and "--delete-branch" in spy.merge_argv
