"""QA 驗收：任務 #2「push 前 ls-remote 防呆檢查」專測。

驗收標準（聚焦本任務，對應總驗收標準 #2）：
- push 前會呼叫 `git ls-remote --heads origin <branch>`。
- 遠端已存在同名分支（ls-remote rc==0 且有輸出）且未開 FORCE_PUSH 時：
  函式回傳 (False, ...) 且「不執行任何 push」。
- ls-remote 本身失敗（rc!=0，網路/認證）：視為錯誤中止，回傳 (False, ...)，不 push。
- 遠端不存在（rc==0 空輸出）：放行，往下走 push。
- dryrun 早回時不打網路（不呼叫 ls-remote）。

手法：攔截 autopilot._run，依指令前綴回傳可控結果並記錄整個呼叫序列；
所有斷言僅靠攔截到的指令序列，全程不發起真實 git/網路操作。
"""

from __future__ import annotations

import asyncio

import pytest

from studio import autopilot, config

_TASK = {"id": "42", "title": "示範任務", "detail": "細節"}
_BRANCH = "autopilot/task-42"


class RunSpy:
    """攔截 autopilot._run 的 async spy：依指令分派可控結果並記錄呼叫序列。

    overrides: dict[str, tuple[int, str]]——以「指令關鍵片段」對應 (rc, output)。
    比對方式：把 cmd 串成字串，第一個命中的 key 即採用其回傳值；預設 (0, "")。
    """

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

    def joined_calls(self) -> list[str]:
        return [" ".join(c) for c in self.calls]

    def called(self, fragment: str) -> bool:
        return any(fragment in j for j in self.joined_calls())

    def index_of(self, fragment: str) -> int:
        for i, j in enumerate(self.joined_calls()):
            if fragment in j:
                return i
        return -1


@pytest.fixture(autouse=True)
def _forbid_real_subprocess(monkeypatch):
    """保險絲：本檔禁止啟動真實子程序，確保所有驗證僅靠攔截序列。"""

    async def _boom(*args, **kwargs):
        raise RuntimeError("test forbids spawning a real subprocess (no network)")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _boom)


@pytest.fixture(autouse=True)
def _safe_config(monkeypatch):
    """預設：非 dryrun、非 force（安全側），讓被測邏輯走完整 push 路徑判定。"""
    monkeypatch.setattr(config, "AUTOPILOT_DRYRUN", False)
    monkeypatch.setattr(config, "AUTOPILOT_FORCE_PUSH", False)
    monkeypatch.setattr(config, "AUTOPILOT_BRANCH", "main")


def _install(monkeypatch, overrides):
    spy = RunSpy(overrides)
    monkeypatch.setattr(autopilot, "_run", spy)
    return spy


# 讓「有變更可合併」恆成立：rev-list --count 回 "1"
_HAS_CHANGE = {"rev-list --count": (0, "1")}


# === 防呆檢查確實在 push 前呼叫 ls-remote ==============================


@pytest.mark.asyncio
async def test_lsremote_called_before_push_when_remote_absent(monkeypatch):
    """遠端不存在（rc==0 空輸出）：放行往下 push，且 ls-remote 在 push 之前。"""
    spy = _install(monkeypatch, {**_HAS_CHANGE})  # ls-remote 預設回 (0,"")
    ok, msg = await autopilot._commit_push_merge("/clone", _TASK)

    # ls-remote 有被呼叫，且指令形態正確
    assert spy.called(f"ls-remote --heads origin {_BRANCH}")
    # push 有被呼叫（遠端不存在 → 放行）
    assert spy.called("push") and spy.called(_BRANCH)
    # 順序：ls-remote 在 push 之前（push 已改為非強制，不再帶 -f）
    assert spy.index_of("ls-remote") < spy.index_of("push")
    assert ok is True


# === 遠端已存在同名分支 → 中止，且絕不 push ===========================


@pytest.mark.asyncio
async def test_remote_branch_exists_aborts_without_push(monkeypatch):
    """遠端已存在同名分支且未開 FORCE_PUSH：回 (False, ...) 且不執行任何 push。"""
    lsremote_out = f"abc123\trefs/heads/{_BRANCH}\n"
    spy = _install(
        monkeypatch,
        {**_HAS_CHANGE, "ls-remote --heads": (0, lsremote_out)},
    )
    ok, msg = await autopilot._commit_push_merge("/clone", _TASK)

    assert ok is False
    # 關鍵：完全沒有任何 push 指令被呼叫（不做任何覆寫）
    assert not spy.called("push"), f"中止情境不應 push，實際呼叫：{spy.joined_calls()}"
    # 也不應走到 gh pr 動作
    assert not spy.called("pr create")
    assert not spy.called("pr merge")
    # 回報訊息明確提示遠端已存在 + 可用 FORCE_PUSH 覆寫
    assert "遠端已存在" in msg
    assert "TI_AUTOPILOT_FORCE_PUSH=1" in msg


# === ls-remote 本身失敗（rc!=0）→ 中止，不可 fall-through ============


@pytest.mark.asyncio
async def test_lsremote_failure_aborts_without_push(monkeypatch):
    """ls-remote rc!=0（網路/認證失敗）：視為錯誤中止，回 (False, ...) 且不 push。"""
    spy = _install(
        monkeypatch,
        {**_HAS_CHANGE, "ls-remote --heads": (128, "fatal: could not read from remote")},
    )
    ok, msg = await autopilot._commit_push_merge("/clone", _TASK)

    assert ok is False
    assert not spy.called("push"), f"ls-remote 失敗不應 fall-through 去 push：{spy.joined_calls()}"
    assert "ls-remote 檢查失敗" in msg or "無法確認遠端狀態" in msg


# === dryrun 早回：不打網路（不呼叫 ls-remote）=========================


@pytest.mark.asyncio
async def test_dryrun_does_not_call_lsremote(monkeypatch):
    """dryrun 時在 ls-remote 之前早回，保住「只回報、不打網路」語意。"""
    monkeypatch.setattr(config, "AUTOPILOT_DRYRUN", True)
    spy = _install(monkeypatch, {**_HAS_CHANGE})
    ok, msg = await autopilot._commit_push_merge("/clone", _TASK)

    assert ok is True
    assert "[dryrun]" in msg
    assert not spy.called("ls-remote"), "dryrun 不應打網路做 ls-remote"
    assert not spy.called("push")


# === 無變更早回：在 ls-remote 之前，不打網路 ==========================


@pytest.mark.asyncio
async def test_no_change_aborts_before_lsremote(monkeypatch):
    """rev-list 為 0（無 commit 可合併）：早回，不呼叫 ls-remote 也不 push。"""
    spy = _install(monkeypatch, {"rev-list --count": (0, "0")})
    ok, msg = await autopilot._commit_push_merge("/clone", _TASK)

    assert ok is False
    assert "沒有產生任何變更" in msg
    assert not spy.called("ls-remote")
    assert not spy.called("push")
