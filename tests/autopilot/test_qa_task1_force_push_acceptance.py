"""QA 驗收：任務 #1 「預設非強制推送；僅 FORCE_PUSH=1 改用 --force-with-lease
--force-if-includes，絕不用裸 -f」。

彙整 6 條驗收標準，全程攔截 autopilot._run，不碰網路：
 1. 全檔無裸 `push -f` / 單獨 `--force`（僅允許 lease + if-includes）。
 2. 預設 push 無 force 旗標；遠端同名分支存在→中止不覆寫。
 3. FORCE_PUSH=1 → push 旗標恰為 --force-with-lease + --force-if-includes。
 4. ls-remote rc!=0 → 中止，不繼續 push。
 5. 合併走 publisher._merge_flow（等 CI→綠才合併），不再有盲合的 gh pr merge。
 6. publisher.py 的 push -u 屬範圍外（記錄註記，不應受 FORCE_PUSH 管轄）。
"""

from __future__ import annotations

import asyncio
import re

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


_ROOT = REPO_ROOT
_SRC = (_ROOT / "studio" / "autopilot.py").read_text(encoding="utf-8")
_TASK = {"id": "1", "title": "t", "detail": "d"}
_BRANCH = "autopilot/task-1"


class RunSpy:
    """攔截 _run；overrides 以子字串比對回傳 (rc, out)。"""

    def __init__(self, overrides=None):
        self.overrides = overrides or {}
        self.calls: list[list[str]] = []

    async def __call__(self, cmd, cwd=None, timeout=600, **kwargs):
        self.calls.append(list(cmd))
        joined = " ".join(cmd)
        for key, val in self.overrides.items():
            if key in joined:
                return val
        if "remote get-url --push origin" in joined:
            return (0, f"https://github.com/{config.AUTOPILOT_REPO}.git")
        return (0, "")

    def argv_with(self, token):
        for c in self.calls:
            if token in c:
                return c
        return []

    @property
    def push_argv(self):
        return self.argv_with("push")

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
    # rev-list 需回傳非 0 數量讓流程繼續到 push
    monkeypatch.setattr(config, "AUTOPILOT_DRYRUN", False)
    monkeypatch.setattr(config, "AUTOPILOT_FORCE_PUSH", False)
    # RECLAIM_BRANCH 固定 False：本檔守護「遠端已存在同名分支＝中止」舊不變式（逃生門），
    # 認領新路徑（預設開）由 test_reclaim_stale_branch.py 覆蓋。
    monkeypatch.setattr(config, "AUTOPILOT_RECLAIM_BRANCH", False)
    monkeypatch.setattr(config, "AUTOPILOT_REPO", "owner/repo")
    # owner allowlist 護欄：放行本檔測試用的 owner
    monkeypatch.setattr(config, "PUBLISH_OWNER_ALLOWLIST", frozenset({"owner"}))


def _spy_for(monkeypatch, overrides=None):
    base = {"rev-list": (0, "3"), "pr view": (0, "7")}  # 有變更可合併＋PR 編號
    base.update(overrides or {})
    spy = RunSpy(base)
    monkeypatch.setattr(autopilot, "_run", spy)
    return spy


# ---- 標準 1：靜態防線 -------------------------------------------------------


def test_std1_no_bare_force_in_source():
    assert not re.search(r"push[^\n]*\s-f(\s|\")", _SRC), "出現裸 push -f"
    # 單獨 --force（非 --force-with-lease / --force-if-includes）
    bare = re.findall(r"--force(?!-with-lease|-if-includes)", _SRC)
    assert not bare, f"出現裸 --force：{bare}"
    assert "--force-with-lease" in _SRC and "--force-if-includes" in _SRC


# ---- 標準 2：預設非強制 + 遠端同名分支中止 ---------------------------------


def test_std2_default_push_no_force_flags(monkeypatch):
    spy = _spy_for(monkeypatch)  # ls-remote 預設回 (0, "") → 不存在
    ok, msg = asyncio.run(autopilot._commit_push_merge("/clone", _TASK))
    assert ok, msg
    argv = spy.push_argv
    assert argv, "未發出 push"
    assert "--force" not in " ".join(argv)
    assert "-f" not in argv
    assert argv[-3:] == ["-u", "origin", _BRANCH]


def test_std2_existing_remote_branch_aborts(monkeypatch):
    spy = _spy_for(monkeypatch, {"ls-remote": (0, "abc123\trefs/heads/" + _BRANCH)})
    ok, msg = asyncio.run(autopilot._commit_push_merge("/clone", _TASK))
    assert not ok
    assert "已中止" in msg or "已存在" in msg
    assert not spy.push_argv, "中止後不應 push（避免覆寫）"


# ---- 標準 3：FORCE_PUSH 開啟 → lease + if-includes 兩者並存 ----------------


def test_std3_force_push_uses_lease_and_if_includes(monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_FORCE_PUSH", True)
    spy = _spy_for(monkeypatch, {"ls-remote": (0, "abc\trefs/heads/" + _BRANCH)})
    ok, msg = asyncio.run(autopilot._commit_push_merge("/clone", _TASK))
    assert ok, msg
    argv = spy.push_argv
    i = argv.index("push")
    assert argv[i + 1 : i + 3] == ["--force-with-lease", "--force-if-includes"]
    assert "-f" not in argv


# ---- 標準 4：ls-remote rc!=0 → 中止 ----------------------------------------


def test_std4_lsremote_failure_aborts(monkeypatch):
    spy = _spy_for(monkeypatch, {"ls-remote": (128, "fatal: auth failed")})
    ok, msg = asyncio.run(autopilot._commit_push_merge("/clone", _TASK))
    assert not ok
    assert "ls-remote" in msg and "中止" in msg
    assert not spy.push_argv, "rc!=0 絕不可 fall-through 去 push"


# ---- 標準 5：合併走 publisher._merge_flow（等 CI→合併），不再盲合 gh pr merge ----


def test_std5_no_blind_pr_merge(monkeypatch):
    """Option 2 後：合併經 publisher._merge_flow（等 CI 綠才合併），不再有盲合的 `gh pr merge`。"""
    spy = _spy_for(monkeypatch)
    ok, _ = asyncio.run(autopilot._commit_push_merge("/clone", _TASK))
    assert ok is True
    assert any("create" in c and "pr" in c for c in spy.calls), "應有開 PR"
    assert not any("merge" in c and "pr" in c for c in spy.calls), "不該再盲合 gh pr merge"


# ---- 標準 6：publisher push 屬範圍外（註記） ------------------------------


def test_std6_publisher_push_out_of_scope():
    pub = (_ROOT / "studio" / "publisher.py").read_text(encoding="utf-8")
    assert "git" in pub and "push" in pub
    # publisher 用獨立 remote（ti_publish），不讀 FORCE_PUSH，設計上不受本防線管轄
    assert "AUTOPILOT_FORCE_PUSH" not in pub
