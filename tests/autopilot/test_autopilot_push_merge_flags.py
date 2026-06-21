"""單元測試：`_commit_push_merge` 的指令組裝與「等 CI→合併」流程。

設計決策（Option 2，2026-06-21）：autopilot 成為唯一發佈者，wrapper 在合併前等 CI。
故 `_commit_push_merge` 不再直接 `gh pr merge --squash`，改為開 PR 後取 PR 編號、
交給 publisher._merge_flow（已測過的協調器）等 CI 綠才合併；非 MERGED 則關 PR 刪分支。

仍保留的 push 防呆覆蓋（push flags / ls-remote 防覆寫 / force 逃生門）：
1. 預設（非 force）：push 非強制，不含 -f / --force / --force-with-lease。
2. 遠端已存在同名分支且非 force：中止、回 (False, ...)，完全不 push。
3. force 開啟（遠端不存在）：push 走 --force-with-lease --force-if-includes，絕無裸 -f。
5. 交集——force 開啟＋遠端已存在：不被 ls-remote 中止，且 push 走 force-with-lease。

新增「等 CI→合併」契約覆蓋：
6. _merge_flow 回 MERGED → 回 (True, ...)，且絕不再呼叫 `gh pr merge`（盲合）。
7. _merge_flow 回非 MERGED（如 CI_FAILED）→ 呼叫 `gh pr close --delete-branch`、回 (False, ...)。
8. PR 編號解析失敗 → 直接回 (False, ...)，不進 _merge_flow（不盲合）。

手法：攔截 autopilot._run 依指令片段回傳可控結果並記錄完整呼叫序列；
monkeypatch publisher._merge_flow 控制合併結局；全程不發起真實 git / 網路操作。
"""

from __future__ import annotations

import asyncio

import pytest

from studio import autopilot, config, publisher

_TASK = {"id": "7", "title": "示範任務", "detail": "細節"}
_BRANCH = "autopilot/task-7"

# 讓「有變更可合併」恆成立：rev-list --count 回 "1"；pr view 回 PR 編號 "42"。
_HAS_CHANGE = {"rev-list --count": (0, "1"), "pr view": (0, "42")}
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


class MergeFlowSpy:
    """攔截 publisher._merge_flow：回傳指定結局，並記錄被呼叫的引數。"""

    def __init__(self, outcome, detail="ok"):
        self.outcome = outcome
        self.detail = detail
        self.calls: list = []

    async def __call__(self, number, payload, **kwargs):
        self.calls.append((number, payload, kwargs))
        return (self.outcome, self.detail)


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
    monkeypatch.setattr(config, "AUTOPILOT_BRANCH", "main")


def _install(monkeypatch, overrides, *, merge_outcome=None, merge_detail="ok"):
    spy = RunSpy(overrides)
    monkeypatch.setattr(autopilot, "_run", spy)
    flow = MergeFlowSpy(
        merge_outcome if merge_outcome is not None else publisher.MergeOutcome.MERGED, merge_detail
    )
    monkeypatch.setattr(publisher, "_merge_flow", flow)
    return spy, flow


# === 情境 1：預設非強制推送 ==========================================


@pytest.mark.asyncio
async def test_default_push_is_non_forced(monkeypatch):
    spy, _ = _install(monkeypatch, {**_HAS_CHANGE})  # ls-remote 預設 (0,"") → 遠端不存在
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
    spy, _ = _install(monkeypatch, {**_HAS_CHANGE, **_REMOTE_EXISTS})
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
    spy, _ = _install(monkeypatch, {**_HAS_CHANGE})
    ok, msg = await autopilot._commit_push_merge("/clone", _TASK)

    push = spy.push_cmd()
    assert push is not None
    assert "--force-with-lease" in push
    assert "--force-if-includes" in push
    assert " -f" not in f" {push} ", "禁止裸 -f"
    assert ok is True


# === 情境 5（交集，死碼漏測）：force 開啟 + 遠端已存在 ================


@pytest.mark.asyncio
async def test_force_push_not_aborted_when_remote_exists(monkeypatch):
    """force 為逃生門：遠端已存在同名分支時不被 ls-remote 中止，且走 force-with-lease。"""
    monkeypatch.setattr(config, "AUTOPILOT_FORCE_PUSH", True)
    spy, _ = _install(monkeypatch, {**_HAS_CHANGE, **_REMOTE_EXISTS})
    ok, msg = await autopilot._commit_push_merge("/clone", _TASK)

    push = spy.push_cmd()
    assert push is not None, f"force 開啟時遠端已存在不應中止：{spy.joined()}"
    assert "--force-with-lease" in push
    assert "--force-if-includes" in push
    assert " -f" not in f" {push} "
    assert ok is True


# === 情境 6：等 CI→合併（MERGED）→ 成功，且絕不盲合 `gh pr merge` =====


@pytest.mark.asyncio
async def test_merged_via_merge_flow_no_blind_pr_merge(monkeypatch):
    spy, flow = _install(monkeypatch, {**_HAS_CHANGE})
    ok, msg = await autopilot._commit_push_merge("/clone", _TASK)

    assert ok is True
    assert spy.called("pr create"), "應有開 PR"
    assert spy.called("pr view"), "應取 PR 編號"
    # 關鍵：不再有盲合的 `gh pr merge`，合併一律走 publisher._merge_flow。
    assert not spy.called("pr merge"), f"不該再盲合 gh pr merge：{spy.joined()}"
    assert len(flow.calls) == 1, "應呼叫 _merge_flow 一次"
    number, payload, kwargs = flow.calls[0]
    assert number == 42, "PR 編號應由 pr view 解析"
    assert payload.get("merge_method") == "squash"
    # 等 CI 的參數確實透傳給協調器
    assert "ci_timeout" in kwargs and "ci_interval" in kwargs and "retries" in kwargs


# === 情境 7：_merge_flow 非 MERGED → 關 PR 刪分支、回 False ============


@pytest.mark.asyncio
async def test_non_merged_closes_pr_and_returns_false(monkeypatch):
    spy, flow = _install(
        monkeypatch,
        {**_HAS_CHANGE},
        merge_outcome=publisher.MergeOutcome.CI_FAILED,
        merge_detail="CI 紅",
    )
    ok, msg = await autopilot._commit_push_merge("/clone", _TASK)

    assert ok is False
    assert len(flow.calls) == 1
    # 留下孤兒 PR 不可接受：非綠時關 PR 並刪分支
    assert spy.called("pr close"), f"非 MERGED 應關 PR：{spy.joined()}"
    assert spy.called("--delete-branch")
    assert "ci_failed" in msg or "CI 紅" in msg


# === 情境 8：PR 編號解析失敗 → 不進 _merge_flow、不盲合 ================


@pytest.mark.asyncio
async def test_pr_number_parse_failure_aborts_before_merge(monkeypatch):
    spy, flow = _install(monkeypatch, {**_HAS_CHANGE, "pr view": (0, "not-a-number")})
    ok, msg = await autopilot._commit_push_merge("/clone", _TASK)

    assert ok is False
    assert flow.calls == [], "解析失敗不該進 _merge_flow"
    assert not spy.called("pr merge"), "解析失敗絕不盲合"
    assert "PR 編號" in msg
