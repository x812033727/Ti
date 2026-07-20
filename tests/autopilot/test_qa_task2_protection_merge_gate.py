"""QA 驗收：任務「將分支保護檢查接進 _commit_push_merge」接點專測。

驗證第二道防線（合併目標 AUTOPILOT_BRANCH 的保護狀態檢查）正確接入 _commit_push_merge：
  - 接點位置：在 squash-merge 之前（架構決策定為 push 之前，unknown 中止不留遠端孤兒分支）。
  - 三態決策：unknown→fail-safe 中止（訊息含「無法確認保護狀態」、不 push）；
              protected / unprotected→放行（照常 push + pr merge）。
  - dryrun：AUTOPILOT_DRYRUN=1 時提早 return，完全不打保護 API。
  - 逃生口：AUTOPILOT_PROTECTION_CHECK=0 時整段跳過。
  - 不衝突：原 ls-remote 防覆寫邏輯不變，兩道防線各自獨立。

對應驗收標準 #3 / #4 / #5。

手法：整合測試——不 mock _check_branch_protection，改攔截 autopilot._run 並依指令
關鍵片段回傳 gh 回應，藉此同時驗證「函式判讀 + 接點決策」的真實串接。全程不發起真實
git/gh/網路操作。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from studio import autopilot, config, publisher


@pytest.fixture(autouse=True)
def _merge_flow_merged(monkeypatch):
    """Option 2 後合併走 publisher._merge_flow（等 CI→合併）。本檔聚焦 push/protection 旗標，
    一律把 _merge_flow 打成回 MERGED，讓 _commit_push_merge 能走完合併段、回 (True, ...)。"""

    async def _merged(number, payload, **kwargs):
        return (publisher.MergeOutcome.MERGED, "sha")

    monkeypatch.setattr(publisher, "_merge_flow", _merged)


_TASK = {"id": "2", "title": "接點驗證任務", "detail": ""}
_BRANCH = "autopilot/task-2"  # task 分支
_MAIN = "main"  # 合併目標（保護檢查的目標）
_REPO = "octo/Ti"

# gh 回應片段
_RULES_PROTECTED = json.dumps([{"type": "pull_request"}])
_RULES_EMPTY = "[]"
_HTTP_404 = "gh: Not Found (HTTP 404)"
_HTTP_403 = "gh: Resource not accessible by integration (HTTP 403)"
_TIMEOUT = "(逾時 60s)"


class RunSpy:
    """攔截 autopilot._run：依指令關鍵片段回傳 (rc, out)，並記錄整個呼叫序列。"""

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

    def called(self, fragment: str) -> bool:
        return any(fragment in " ".join(c) for c in self.calls)

    def index_of(self, fragment: str) -> int:
        for i, c in enumerate(self.calls):
            if fragment in " ".join(c):
                return i
        return -1


@pytest.fixture(autouse=True)
def _forbid_real_subprocess(monkeypatch):
    async def _boom(*args, **kwargs):
        raise RuntimeError("test forbids spawning a real subprocess (no network)")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _boom)


@pytest.fixture(autouse=True)
def _base_config(monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_DRYRUN", False)
    monkeypatch.setattr(config, "AUTOPILOT_BRANCH", _MAIN)
    monkeypatch.setattr(config, "AUTOPILOT_REPO", _REPO)
    monkeypatch.setattr(config, "AUTOPILOT_FORCE_PUSH", False)
    # RECLAIM_BRANCH 固定 False：本檔守護「遠端已存在同名分支＝中止」舊不變式（逃生門），
    # 認領新路徑（預設開）由 test_reclaim_stale_branch.py 覆蓋。
    monkeypatch.setattr(config, "AUTOPILOT_RECLAIM_BRANCH", False)
    monkeypatch.setattr(config, "AUTOPILOT_PROTECTION_CHECK", True)
    # owner allowlist 護欄：放行本檔測試用的 owner
    monkeypatch.setattr(config, "PUBLISH_OWNER_ALLOWLIST", frozenset({"octo"}))


async def _run_merge(monkeypatch, protection_overrides):
    """以指定保護端點回應跑一次 _commit_push_merge，回傳 (ok, msg, spy)。"""
    overrides = {
        "rev-list --count": (0, "1"),  # 恆有變更可合併
        "ls-remote --heads": (0, ""),  # 遠端無同名分支（放行 push）
        "pr view": (0, "7"),  # PR 編號（合併走 publisher._merge_flow，conftest 預設回 MERGED）
    }
    overrides.update(protection_overrides)
    spy = RunSpy(overrides)
    monkeypatch.setattr(autopilot, "_run", spy)
    ok, msg = await autopilot._commit_push_merge("/clone", _TASK)
    return ok, msg, spy


# === 接點 × 四態 =========================================================


@pytest.mark.asyncio
async def test_protected_allows_merge(monkeypatch):
    """有保護（Rulesets 非空）→ 放行，照常 push + pr merge。"""
    ok, msg, spy = await _run_merge(monkeypatch, {"rules/branches": (0, _RULES_PROTECTED)})
    assert ok is True, msg
    # 合併走 publisher._merge_flow（等 CI→合併），不再盲合 `gh pr merge`；放行表現為有開 PR 且 ok。
    assert spy.called("push") and spy.called("pr create") and not spy.called("pr merge")
    # 端點正確：查的是合併目標 main，不是 task 分支
    assert spy.called(f"repos/{_REPO}/rules/branches/{_MAIN}")
    assert not spy.called("rules/branches/autopilot/task-")


@pytest.mark.asyncio
async def test_unprotected_404_allows_merge(monkeypatch):
    """明確無保護（Rulesets 空 + 舊端點 404）→ 放行。"""
    ok, msg, spy = await _run_merge(
        monkeypatch, {"rules/branches": (0, _RULES_EMPTY), "/protection": (1, _HTTP_404)}
    )
    assert ok is True, msg
    assert spy.called("push") and spy.called("pr create") and not spy.called("pr merge")


@pytest.mark.asyncio
async def test_unknown_403_aborts_before_push(monkeypatch):
    """403 無權限→unknown→fail-safe 中止：不 push、不 merge，訊息含關鍵字樣。"""
    ok, msg, spy = await _run_merge(monkeypatch, {"rules/branches": (1, _HTTP_403)})
    assert ok is False
    assert "無法確認保護狀態" in msg, f"訊息須含明確字樣：{msg}"
    assert not spy.called("push"), f"unknown 必須在 push 前中止：{spy.calls}"
    assert not spy.called("pr merge")
    # 逃生口提示
    assert "TI_AUTOPILOT_PROTECTION_CHECK=0" in msg


@pytest.mark.asyncio
async def test_unknown_timeout_aborts(monkeypatch):
    """網路/逾時→unknown→中止。"""
    ok, msg, spy = await _run_merge(monkeypatch, {"rules/branches": (-1, _TIMEOUT)})
    assert ok is False
    assert "無法確認保護狀態" in msg
    assert not spy.called("push")


# === 接點位置：保護檢查在 push 之前、squash-merge 之前 ====================


@pytest.mark.asyncio
async def test_protection_check_runs_before_push_and_merge(monkeypatch):
    ok, msg, spy = await _run_merge(monkeypatch, {"rules/branches": (0, _RULES_PROTECTED)})
    assert ok is True, msg
    i_check = spy.index_of("rules/branches")
    i_push = spy.index_of("push")
    # 合併不再盲合 `gh pr merge`，改以 `pr create`（開 PR）為合併階段的可觀測接點。
    i_merge = spy.index_of("pr create")
    assert i_check != -1 and i_push != -1 and i_merge != -1
    assert i_check < i_push < i_merge, f"順序須為 保護檢查→push→開 PR：{spy.calls}"
    # 同時在 ls-remote 之後（兩道防線串接順序）
    assert spy.index_of("ls-remote") < i_check


# === dryrun 不打 API =====================================================


@pytest.mark.asyncio
async def test_dryrun_does_not_call_protection_api(monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_DRYRUN", True)
    # 即使保護端點會回 403，dryrun 也不該呼叫到它
    spy = RunSpy({"rev-list --count": (0, "1"), "rules/branches": (1, _HTTP_403)})
    monkeypatch.setattr(autopilot, "_run", spy)
    ok, msg = await autopilot._commit_push_merge("/clone", _TASK)
    assert ok is True, msg
    assert "[dryrun]" in msg
    assert not spy.called("rules/branches"), "dryrun 不應呼叫保護 API"
    assert not spy.called("/protection")
    assert not spy.called("push") and not spy.called("pr merge")


# === 逃生口：PROTECTION_CHECK=0 整段跳過 =================================


@pytest.mark.asyncio
async def test_protection_check_disabled_skips_api(monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_PROTECTION_CHECK", False)
    # 關閉開關後，即使端點會回 403（unknown），也不該被呼叫，照常 push+merge
    ok, msg, spy = await _run_merge(monkeypatch, {"rules/branches": (1, _HTTP_403)})
    assert ok is True, msg
    assert not spy.called("rules/branches"), "關閉開關應整段跳過保護 API"
    assert not spy.called("/protection")
    assert spy.called("push") and spy.called("pr create") and not spy.called("pr merge")


# === 不衝突：ls-remote 防覆寫獨立運作 ====================================


@pytest.mark.asyncio
async def test_lsremote_failure_aborts_before_protection_check(monkeypatch):
    """ls-remote 失敗（既有第一道防線）→ 中止，且根本不會走到保護檢查。"""
    spy = RunSpy(
        {
            "rev-list --count": (0, "1"),
            "ls-remote --heads": (128, "fatal: could not read from remote"),
            "rules/branches": (1, _HTTP_403),
        }
    )
    monkeypatch.setattr(autopilot, "_run", spy)
    ok, msg = await autopilot._commit_push_merge("/clone", _TASK)
    assert ok is False
    assert "ls-remote 檢查失敗" in msg or "無法確認遠端狀態" in msg
    assert not spy.called("rules/branches"), "ls-remote 先中止，不應進保護檢查"
    assert not spy.called("push")


@pytest.mark.asyncio
async def test_remote_branch_exists_aborts_independent_of_protection(monkeypatch):
    """遠端已存在同名分支（未開 FORCE_PUSH）→ 中止，保護檢查不影響此既有行為。"""
    spy = RunSpy(
        {
            "rev-list --count": (0, "1"),
            "ls-remote --heads": (0, f"deadbeef\trefs/heads/{_BRANCH}\n"),
            "rules/branches": (0, _RULES_PROTECTED),
        }
    )
    monkeypatch.setattr(autopilot, "_run", spy)
    ok, msg = await autopilot._commit_push_merge("/clone", _TASK)
    assert ok is False
    assert "遠端已存在" in msg
    assert not spy.called("push")
