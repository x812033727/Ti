"""QA 驗收：任務 #4「gh pr merge --admin 改受 AUTOPILOT_MERGE_ADMIN 旗標控制」專測。

驗收標準（對應總驗收標準 #4）：
- `gh pr merge` 預設不帶 `--admin`（讓 GitHub 分支保護/必過檢查生效）。
- 僅 AUTOPILOT_MERGE_ADMIN 為真時才加上 `--admin`。
- merge 仍維持 --squash --delete-branch 等既有行為。
- source-level：repo 不得殘留寫死的 `--admin`。

手法：攔截 autopilot._run，擷取實際 `pr merge` 指令的 argv 來斷言旗標組合。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from studio import autopilot, config

_TASK = {"id": "9", "title": "示範任務", "detail": ""}
_BRANCH = "autopilot/task-9"
_REPO_ROOT = Path(__file__).resolve().parent.parent


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

    def merge_argv(self) -> list[str]:
        """回傳實際 `pr merge` 指令的 argv。找不到回空。"""
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
    # 非 dryrun、非 force、遠端不存在（讓流程走到 merge）
    monkeypatch.setattr(config, "AUTOPILOT_DRYRUN", False)
    monkeypatch.setattr(config, "AUTOPILOT_FORCE_PUSH", False)
    monkeypatch.setattr(config, "AUTOPILOT_BRANCH", "main")


def _install(monkeypatch, overrides):
    spy = RunSpy(overrides)
    monkeypatch.setattr(autopilot, "_run", spy)
    return spy


_HAS_CHANGE = {"rev-list --count": (0, "1")}


# === 驗收 #4：預設不帶 --admin ========================================


@pytest.mark.asyncio
async def test_default_merge_has_no_admin(monkeypatch):
    """預設（MERGE_ADMIN=False）：pr merge 不含 --admin，讓分支保護生效。"""
    monkeypatch.setattr(config, "AUTOPILOT_MERGE_ADMIN", False)
    spy = _install(monkeypatch, {**_HAS_CHANGE})
    ok, msg = await autopilot._commit_push_merge("/clone", _TASK)

    argv = spy.merge_argv()
    assert argv, "未呼叫 pr merge"
    assert "--admin" not in argv, f"預設不應帶 --admin，實際：{argv}"
    # 既有行為維持
    assert "--squash" in argv
    assert "--delete-branch" in argv
    assert _BRANCH in argv
    assert ok is True


# === 驗收 #4：旗標開啟才帶 --admin ====================================


@pytest.mark.asyncio
async def test_merge_admin_flag_on(monkeypatch):
    """MERGE_ADMIN=True：pr merge 帶上 --admin（繞過分支保護）。"""
    monkeypatch.setattr(config, "AUTOPILOT_MERGE_ADMIN", True)
    spy = _install(monkeypatch, {**_HAS_CHANGE})
    ok, msg = await autopilot._commit_push_merge("/clone", _TASK)

    argv = spy.merge_argv()
    assert argv, "未呼叫 pr merge"
    assert "--admin" in argv, f"MERGE_ADMIN 開啟時應帶 --admin，實際：{argv}"
    assert "--squash" in argv
    assert "--delete-branch" in argv
    assert ok is True


# === merge 失敗訊息保留 gh 原始輸出（架構決策）========================


@pytest.mark.asyncio
async def test_merge_failure_keeps_gh_output(monkeypatch):
    """預設無 --admin 時若被分支保護擋下，回 (False, ...) 且保留 gh 原始輸出便於診斷。"""
    monkeypatch.setattr(config, "AUTOPILOT_MERGE_ADMIN", False)
    gh_err = "GraphQL: Branch protection rules not satisfied"
    spy = _install(monkeypatch, {**_HAS_CHANGE, "pr merge": (1, gh_err)})
    ok, msg = await autopilot._commit_push_merge("/clone", _TASK)

    assert ok is False
    assert "merge 失敗" in msg
    assert gh_err in msg


# === source-level：repo 不得殘留寫死的 --admin ========================


def test_no_hardcoded_admin_in_source():
    """autopilot.py 的 --admin 必須由旗標 gate，不得寫死在 merge 指令字面。"""
    text = _REPO_ROOT.joinpath("studio", "autopilot.py").read_text(encoding="utf-8")
    # 寫死樣態：merge 指令裡直接出現 "--admin" 字面（非經 admin_flag 變數）
    assert '"--squash", "--admin"' not in text
    assert "'--squash', '--admin'" not in text
    # 正面：應由旗標構造 admin_flag
    assert "AUTOPILOT_MERGE_ADMIN" in text
