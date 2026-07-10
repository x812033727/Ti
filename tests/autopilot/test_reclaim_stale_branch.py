"""單元測試：`_reclaim_stale_branch` 殘留分支認領（完成率第三輪 B-4）。

背景：任務分支名由 task id 決定（autopilot/task-{id}）。前次執行在「等 CI→合併」期間
被中斷（SIGTERM/execv 重載/crash）會留下遠端分支（可能連同 open PR）；重跑走到 push 前
防呆撞見同名分支，舊行為一律中止＝任務被自己的殘留永久擋死、殘留 PR 無人認領。

新行為（TI_AUTOPILOT_RECLAIM_BRANCH，預設開）：
1. 殘留分支有 open PR → `gh pr close --delete-branch` 收掉後照常 push＋開新 PR。
2. 殘留分支無 open PR（從未開出/已關閉/已合併但分支殘留）→ 直接刪遠端分支後照常出貨。
3. 刪除失敗（網路/權限）→ 維持既有中止語意（fail-safe 不變），note 經 _merge_fail_note
   標記：暫時性失敗（網路）可被 triage 分診重試。
4. 旋鈕關閉 → 完全恢復「偵測到殘留即中止」舊行為（由 test_autopilot_push_merge_flags.py
   情境 2 守護；本檔僅驗旋鈕分支存在）。

手法沿用 test_autopilot_push_merge_flags.py：攔截 autopilot._run 依指令片段分派結果並
記錄呼叫序列；monkeypatch publisher._merge_flow；全程零真實子程序/網路。
"""

from __future__ import annotations

import asyncio

import pytest

from studio import autopilot, config, publisher

_TASK = {"id": "7", "title": "示範任務", "detail": "細節"}
_BRANCH = "autopilot/task-7"

_HAS_CHANGE = {"rev-list --count": (0, "1"), "--json number": (0, "42")}
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
        if "remote get-url --push origin" in joined:
            return (0, f"https://github.com/{config.AUTOPILOT_REPO}.git")
        return (0, "")

    def joined(self) -> list[str]:
        return [" ".join(c) for c in self.calls]

    def called(self, fragment: str) -> bool:
        return any(fragment in j for j in self.joined())


@pytest.fixture(autouse=True)
def _forbid_real_subprocess(monkeypatch):
    async def _boom(*args, **kwargs):
        raise RuntimeError("test forbids spawning a real subprocess (no network)")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _boom)


@pytest.fixture(autouse=True)
def _base_config(monkeypatch):
    """認領開啟（新預設）、非 force、非 dryrun。"""
    monkeypatch.setattr(config, "AUTOPILOT_DRYRUN", False)
    monkeypatch.setattr(config, "AUTOPILOT_FORCE_PUSH", False)
    monkeypatch.setattr(config, "AUTOPILOT_RECLAIM_BRANCH", True)
    monkeypatch.setattr(config, "AUTOPILOT_BRANCH", "main")


def _install(monkeypatch, overrides):
    spy = RunSpy(overrides)
    monkeypatch.setattr(autopilot, "_run", spy)

    async def _merged(number, payload, **kwargs):
        return (publisher.MergeOutcome.MERGED, "ok")

    monkeypatch.setattr(publisher, "_merge_flow", _merged)
    return spy


# === 情境 1：殘留分支帶 open PR → 關 PR 後照常出貨 ====================


@pytest.mark.asyncio
async def test_reclaim_closes_open_pr_then_ships(monkeypatch):
    spy = _install(
        monkeypatch,
        {**_HAS_CHANGE, **_REMOTE_EXISTS, "--json state": (0, "OPEN")},
    )
    ok, msg = await autopilot._commit_push_merge("/clone", _TASK)

    assert ok is True, f"認領後應照常出貨：{msg}"
    assert spy.called("pr close"), f"應先關掉殘留 open PR：{spy.joined()}"
    assert spy.called("pr create"), "認領後應開新 PR"
    # 已由 pr close --delete-branch 收掉分支，不應再走 git push --delete
    assert not spy.called("push origin --delete")


# === 情境 2：殘留分支無 open PR → 刪遠端分支後照常出貨 ================


@pytest.mark.asyncio
async def test_reclaim_deletes_branch_without_pr(monkeypatch):
    # gh pr view 對「該分支無 PR」回非零（gh 實際行為：no pull requests found）
    spy = _install(
        monkeypatch,
        {
            **_HAS_CHANGE,
            **_REMOTE_EXISTS,
            "--json state": (1, "no pull requests found"),
        },
    )
    ok, msg = await autopilot._commit_push_merge("/clone", _TASK)

    assert ok is True, f"認領後應照常出貨：{msg}"
    assert spy.called("push origin --delete"), f"應刪除殘留遠端分支：{spy.joined()}"
    assert not spy.called("pr close")
    assert spy.called("pr create")


# === 情境 3：刪除失敗 → 維持中止語意，暫時性失敗帶分診標記 =============


@pytest.mark.asyncio
async def test_reclaim_failure_aborts_with_infra_note(monkeypatch):
    spy = _install(
        monkeypatch,
        {
            **_HAS_CHANGE,
            **_REMOTE_EXISTS,
            "--json state": (1, "no pull requests found"),
            "push origin --delete": (1, "fatal: unable to access remote"),
        },
    )
    ok, msg = await autopilot._commit_push_merge("/clone", _TASK)

    assert ok is False
    assert not spy.called("pr create"), "認領失敗不應繼續出貨"
    assert "已中止" in msg
    # 網路暫時性失敗 → _merge_fail_note 附 unreachable，triage 可分診重試
    from studio import backlog

    assert backlog.INFRA_FAILURE_RE.search(msg)


# === 情境 4：認領失敗但原因是權限（非暫時性）→ 不帶分診標記 ============


@pytest.mark.asyncio
async def test_reclaim_failure_permission_no_infra_note(monkeypatch):
    spy = _install(
        monkeypatch,
        {
            **_HAS_CHANGE,
            **_REMOTE_EXISTS,
            "--json state": (0, "OPEN"),
            "pr close": (1, "permission denied (403)"),
        },
    )
    ok, msg = await autopilot._commit_push_merge("/clone", _TASK)

    assert ok is False
    assert not spy.called("pr create")
    assert "unreachable" not in msg, "實質失敗不應附分診標記（會無限重排）"


# === 情境 5：旋鈕關閉 → 舊中止行為 ====================================


@pytest.mark.asyncio
async def test_reclaim_disabled_falls_back_to_abort(monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_RECLAIM_BRANCH", False)
    spy = _install(monkeypatch, {**_HAS_CHANGE, **_REMOTE_EXISTS})
    ok, msg = await autopilot._commit_push_merge("/clone", _TASK)

    assert ok is False
    assert "TI_AUTOPILOT_FORCE_PUSH=1" in msg
    assert not spy.called("pr close")
    assert not spy.called("push origin --delete")
    assert not spy.called("pr create")
