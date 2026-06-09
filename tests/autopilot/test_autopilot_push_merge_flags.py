"""單元測試：`_commit_push_merge` 的指令組裝（五情境）。

對應設計決策——測試情境由四項擴為五項，特別補上「force 開啟＋遠端已存在分支」
的交集情境，覆蓋 force gate 的死碼漏測：

1. 預設（非 force）：push 非強制，不含 -f / --force / --force-with-lease。
2. 遠端已存在同名分支且非 force：中止、回 (False, ...)，完全不 push。
3. force 開啟（遠端不存在）：push 走 --force-with-lease --force-if-includes，絕無裸 -f。
4. merge --admin 開關：預設不帶 --admin；MERGE_ADMIN 為真才帶。
5. 交集——force 開啟＋遠端已存在：不被 ls-remote 中止，且 push 走 force-with-lease。

手法：攔截 autopilot._run，依指令片段回傳可控結果並記錄完整呼叫序列；
全程不發起真實 git / 網路操作。
"""

from __future__ import annotations

import asyncio

import pytest

from studio import autopilot, config

_TASK = {"id": "7", "title": "示範任務", "detail": "細節"}
_BRANCH = "autopilot/task-7"

# 讓「有變更可合併」恆成立：rev-list --count 回 "1"
_HAS_CHANGE = {"rev-list --count": (0, "1")}
# 遠端已存在同名分支的 ls-remote 輸出
_REMOTE_EXISTS = {"ls-remote --heads": (0, f"abc123\trefs/heads/{_BRANCH}\n")}


class RunSpy:
    """攔截 autopilot._run：依指令關鍵片段分派 (rc, output)，並記錄呼叫序列。"""

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

    def joined(self) -> list[str]:
        return [" ".join(c) for c in self.calls]

    def called(self, fragment: str) -> bool:
        return any(fragment in j for j in self.joined())

    def push_cmd(self) -> str | None:
        for j in self.joined():
            if " push " in f" {j} ":
                return j
        return None

    def merge_cmd(self) -> str | None:
        for j in self.joined():
            if "pr merge" in j:
                return j
        return None


@pytest.fixture(autouse=True)
def _forbid_real_subprocess(monkeypatch):
    """保險絲：禁止啟動真實子程序，確保驗證僅靠攔截序列。"""

    async def _boom(*args, **kwargs):
        raise RuntimeError("test forbids spawning a real subprocess (no network)")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _boom)


@pytest.fixture(autouse=True)
def _base_config(monkeypatch):
    """預設安全側：非 dryrun、非 force、非 admin。"""
    monkeypatch.setattr(config, "AUTOPILOT_DRYRUN", False)
    monkeypatch.setattr(config, "AUTOPILOT_FORCE_PUSH", False)
    monkeypatch.setattr(config, "AUTOPILOT_MERGE_ADMIN", False)
    monkeypatch.setattr(config, "AUTOPILOT_BRANCH", "main")


def _install(monkeypatch, overrides):
    spy = RunSpy(overrides)
    monkeypatch.setattr(autopilot, "_run", spy)
    return spy


# === 情境 1：預設非強制推送 ==========================================


@pytest.mark.asyncio
async def test_default_push_is_non_forced(monkeypatch):
    spy = _install(monkeypatch, {**_HAS_CHANGE})  # ls-remote 預設 (0,"") → 遠端不存在
    ok, msg = await autopilot._commit_push_merge("/clone", _TASK)

    push = spy.push_cmd()
    assert push is not None
    assert " -f" not in f" {push} "
    assert "--force" not in push
    assert "--force-with-lease" not in push
    assert f"-u origin {_BRANCH}" in push
    assert ok is True


# === 情境 2：遠端已存在 + 非 force → 中止且不 push ====================


@pytest.mark.asyncio
async def test_remote_exists_aborts_without_push(monkeypatch):
    spy = _install(monkeypatch, {**_HAS_CHANGE, **_REMOTE_EXISTS})
    ok, msg = await autopilot._commit_push_merge("/clone", _TASK)

    assert ok is False
    assert spy.push_cmd() is None, f"中止情境不應 push：{spy.joined()}"
    assert not spy.called("pr create")
    assert not spy.called("pr merge")
    assert "TI_AUTOPILOT_FORCE_PUSH=1" in msg


# === 情境 3：force 開啟（遠端不存在）→ force-with-lease + if-includes ==


@pytest.mark.asyncio
async def test_force_push_uses_lease_and_if_includes(monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_FORCE_PUSH", True)
    spy = _install(monkeypatch, {**_HAS_CHANGE})
    ok, msg = await autopilot._commit_push_merge("/clone", _TASK)

    push = spy.push_cmd()
    assert push is not None
    assert "--force-with-lease" in push
    assert "--force-if-includes" in push
    assert " -f" not in f" {push} ", "禁止裸 -f"
    assert ok is True


# === 情境 4：merge --admin 開關 =====================================


@pytest.mark.asyncio
async def test_merge_admin_default_off(monkeypatch):
    spy = _install(monkeypatch, {**_HAS_CHANGE})
    await autopilot._commit_push_merge("/clone", _TASK)

    merge = spy.merge_cmd()
    assert merge is not None
    assert "--admin" not in merge
    assert "--squash" in merge and "--delete-branch" in merge


@pytest.mark.asyncio
async def test_merge_admin_on_when_flag(monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_MERGE_ADMIN", True)
    spy = _install(monkeypatch, {**_HAS_CHANGE})
    await autopilot._commit_push_merge("/clone", _TASK)

    merge = spy.merge_cmd()
    assert merge is not None
    assert "--admin" in merge


# === 情境 5（交集，死碼漏測）：force 開啟 + 遠端已存在 ================


@pytest.mark.asyncio
async def test_force_push_not_aborted_when_remote_exists(monkeypatch):
    """force 為逃生門：遠端已存在同名分支時不被 ls-remote 中止，且走 force-with-lease。"""
    monkeypatch.setattr(config, "AUTOPILOT_FORCE_PUSH", True)
    spy = _install(monkeypatch, {**_HAS_CHANGE, **_REMOTE_EXISTS})
    ok, msg = await autopilot._commit_push_merge("/clone", _TASK)

    push = spy.push_cmd()
    assert push is not None, f"force 開啟時遠端已存在不應中止：{spy.joined()}"
    assert "--force-with-lease" in push
    assert "--force-if-includes" in push
    assert " -f" not in f" {push} "
    assert ok is True
