"""QA 驗收：任務 #9（總驗收標準 #3）「強制推送僅旗標開啟時觸發，且 lease + if-includes，無裸 -f」。

精確釘死 push argv 與旗標 gate 雙向行為：
- FORCE_PUSH=False：push argv 精確為 git <cred> push -u origin <branch>，無任何 force token。
- FORCE_PUSH=True：push argv 精確為
  git <cred> push --force-with-lease --force-if-includes -u origin <branch>，
  兩旗標相鄰且緊接在 push 之後；不得含裸 -f / 裸 --force。
- 即使遠端已存在分支，FORCE_PUSH=True 仍走 force 路徑（force 非死碼）。
- source-level：force 旗標由 config.AUTOPILOT_FORCE_PUSH gate，不寫死。

全程攔截 autopilot._run，不碰網路。
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


_GIT_CRED = ["-c", "credential.helper=!gh auth git-credential"]
_ROOT = REPO_ROOT
_TASK = {"id": "9", "title": "t", "detail": ""}
_BRANCH = "autopilot/task-9"


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
        if "remote get-url --push origin" in joined:
            return (0, f"https://github.com/{config.AUTOPILOT_REPO}.git")
        return (0, "")

    @property
    def push_argv(self):
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


# === Gate：旗標關閉時 push argv 精確無 force ==========================


@pytest.mark.asyncio
async def test_force_off_exact_argv(monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_FORCE_PUSH", False)
    spy = _install(monkeypatch, {**_HAS_CHANGE})  # 遠端不存在
    ok, _ = await autopilot._commit_push_merge("/clone", _TASK)
    assert ok is True
    assert spy.push_argv == ["git", *_GIT_CRED, "push", "-u", "origin", _BRANCH]


# === 旗標開啟時 push argv 精確含 lease + if-includes ==================


@pytest.mark.asyncio
async def test_force_on_exact_argv(monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_FORCE_PUSH", True)
    # 遠端已存在也要放行（force 非死碼）
    spy = _install(
        monkeypatch, {**_HAS_CHANGE, "ls-remote --heads": (0, f"x\trefs/heads/{_BRANCH}\n")}
    )
    ok, _ = await autopilot._commit_push_merge("/clone", _TASK)
    assert ok is True
    assert spy.push_argv == [
        "git",
        *_GIT_CRED,
        "push",
        "--force-with-lease",
        "--force-if-includes",
        "-u",
        "origin",
        _BRANCH,
    ]


# === 旗標開啟：兩 force 旗標相鄰、緊接 push 之後，且無裸 -f/--force ===


@pytest.mark.asyncio
async def test_force_flags_adjacent_and_no_bare(monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_FORCE_PUSH", True)
    spy = _install(monkeypatch, {**_HAS_CHANGE})
    await autopilot._commit_push_merge("/clone", _TASK)
    argv = spy.push_argv

    i = argv.index("push")
    # push 之後緊接 --force-with-lease，再緊接 --force-if-includes
    assert argv[i + 1] == "--force-with-lease"
    assert argv[i + 2] == "--force-if-includes"
    # 不得使用裸 -f 或裸 --force token
    assert "-f" not in argv
    assert "--force" not in argv
    # --force-with-lease 不可被誤寫成 lease=<ref> 之外的省略；兩者皆完整 token
    assert "--force-with-lease" in argv and "--force-if-includes" in argv


# === source-level：force 由 config gate，不寫死 =======================


def test_force_gated_by_config_in_source():
    text = (_ROOT / "studio" / "autopilot.py").read_text(encoding="utf-8")
    # 兩旗標皆出現，且明確由 AUTOPILOT_FORCE_PUSH 條件控制
    assert "--force-with-lease" in text and "--force-if-includes" in text
    assert "config.AUTOPILOT_FORCE_PUSH" in text
    # 不得殘留裸 push -f / "-f" token
    assert "push -f" not in text
    assert '"-f"' not in text
