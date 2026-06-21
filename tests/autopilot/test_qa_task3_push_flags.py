"""QA 驗收：任務 #3「push 改非強制；旗標開啟才 --force-with-lease --force-if-includes」專測。

驗收標準（對應總驗收標準 #1、#3）：
1. 預設設定下，push 指令不含 `-f`／`--force`，且不含裸 `--force`。
3. 強制推送只在 AUTOPILOT_FORCE_PUSH 開啟時觸發，且使用 `--force-with-lease`
   搭配 `--force-if-includes`，不得使用裸 `-f`。
- 另含 source-level grep：整個 repo 不得殘留 `push -f` / `push", "-f`。

手法：攔截 autopilot._run，依指令分派可控結果並擷取實際 push 指令的 argv。
"""

from __future__ import annotations

import asyncio

import pytest
from _repo import REPO_ROOT

from studio import autopilot, config, publisher


@pytest.fixture(autouse=True)
def _merge_flow_merged(monkeypatch):
    """Option 2 後合併走 publisher._merge_flow（等 CI→合併）。本檔聚焦 push/protection 旗標，
    一律把 _merge_flow 打成回 MERGED，讓 _commit_push_merge 能走完合併段、回 (True, ...)。"""

    async def _merged(number, payload, **kwargs):
        return (publisher.MergeOutcome.MERGED, "sha")

    monkeypatch.setattr(publisher, "_merge_flow", _merged)


_TASK = {"id": "7", "title": "示範任務", "detail": ""}
_BRANCH = "autopilot/task-7"
_REPO_ROOT = REPO_ROOT


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

    def push_argv(self) -> list[str]:
        """回傳實際 push 指令的 argv（git ... push ...）。找不到回空。"""
        for c in self.calls:
            if "push" in c:
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
    monkeypatch.setattr(config, "AUTOPILOT_BRANCH", "main")


def _install(monkeypatch, overrides):
    spy = RunSpy(overrides)
    monkeypatch.setattr(autopilot, "_run", spy)
    return spy


_HAS_CHANGE = {"rev-list --count": (0, "1"), "pr view": (0, "7")}


# === 驗收 #1：預設非強制推送 ==========================================


@pytest.mark.asyncio
async def test_default_push_is_non_forced(monkeypatch):
    """預設（FORCE_PUSH=False）：push 為 `git push -u origin <branch>`，
    不含 -f / --force / --force-with-lease / --force-if-includes。"""
    monkeypatch.setattr(config, "AUTOPILOT_FORCE_PUSH", False)
    spy = _install(monkeypatch, {**_HAS_CHANGE})  # ls-remote 預設 (0,"")：遠端不存在
    ok, msg = await autopilot._commit_push_merge("/clone", _TASK)

    argv = spy.push_argv()
    assert argv, "未呼叫 push"
    # 形態正確：push -u origin <branch>
    assert "push" in argv and "-u" in argv and "origin" in argv and _BRANCH in argv
    # 絕無任何強制旗標
    assert "-f" not in argv
    assert "--force" not in argv
    assert "--force-with-lease" not in argv
    assert "--force-if-includes" not in argv
    assert ok is True


# === 驗收 #3：旗標開啟才強制，且為 lease + if-includes，無裸 -f ========


@pytest.mark.asyncio
async def test_force_flag_uses_lease_and_if_includes(monkeypatch):
    """FORCE_PUSH=True 且遠端已存在分支：不中止，push 帶 --force-with-lease
    搭配 --force-if-includes，且不得使用裸 -f / --force。"""
    monkeypatch.setattr(config, "AUTOPILOT_FORCE_PUSH", True)
    spy = _install(
        monkeypatch,
        {**_HAS_CHANGE, "ls-remote --heads": (0, f"abc\trefs/heads/{_BRANCH}\n")},
    )
    ok, msg = await autopilot._commit_push_merge("/clone", _TASK)

    argv = spy.push_argv()
    assert argv, "FORCE_PUSH 開啟＋遠端已存在時應放行 push（force 不可變死碼）"
    # 兩個旗標都在
    assert "--force-with-lease" in argv
    assert "--force-if-includes" in argv
    # 不得使用裸 -f 或裸 --force
    assert "-f" not in argv
    assert "--force" not in argv  # 注意：--force-with-lease 是獨立 token，不等於 "--force"
    assert ok is True


@pytest.mark.asyncio
async def test_force_off_remote_absent_still_non_forced(monkeypatch):
    """FORCE_PUSH=False 且遠端不存在：正常非強制 push。"""
    monkeypatch.setattr(config, "AUTOPILOT_FORCE_PUSH", False)
    spy = _install(monkeypatch, {**_HAS_CHANGE})
    ok, msg = await autopilot._commit_push_merge("/clone", _TASK)
    argv = spy.push_argv()
    assert "--force-with-lease" not in argv
    assert "-f" not in argv
    assert ok is True


# === 驗收 #1（source-level）：repo 不得殘留裸 push -f ==================


def test_no_bare_push_f_in_source():
    """grep 整個 studio/ 原始碼，不得殘留 `push -f` 或 `push", "-f`。"""
    offenders = []
    for py in _REPO_ROOT.joinpath("studio").rglob("*.py"):
        text = py.read_text(encoding="utf-8")
        if "push -f" in text or '"push", "-f"' in text or "'push', '-f'" in text:
            offenders.append(str(py.relative_to(_REPO_ROOT)))
    assert not offenders, f"仍有裸 push -f 殘留於：{offenders}"
