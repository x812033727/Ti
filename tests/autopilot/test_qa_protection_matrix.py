"""QA 驗收：第二道防線「合併目標分支保護檢查」四態矩陣 + 接點行為專測。

對應驗收標準 #1/#2/#4：
- 端點正確（#1）：檢查目標為 config.AUTOPILOT_BRANCH（main），非 task 分支；
  優先打 Rulesets 端點 `rules/branches/main`，舊 `branches/main/protection` 為輔。
- 三態明確（#2）：protected／unprotected／unknown，且 404→無保護、403/網路/逾時→不確定。
- 接點正確（#4）：unknown→中止；protected/unprotected→放行；dryrun 不打 API；
  TI_AUTOPILOT_PROTECTION_CHECK=0 整段跳過。

與 test_qa_protection_failsafe.py 互補：該檔聚焦「不確定絕不 fall-through」反例矩陣，
本檔聚焦四態正向辨識、端點/分支正確性與接點放行/中止/跳過行為。

手法：攔截 autopilot._run，依指令關鍵片段回傳 gh (rc, out)，記錄完整 argv 序列。
全程不發起真實 git/gh/網路操作。
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


_REPO = "octo/Ti"
_MAIN = "main"
_TASK = {"id": "4", "title": "保護檢查矩陣", "detail": ""}
_TASK_BRANCH = "autopilot/task-4"

# gh api 端點片段
_RULES_EP = f"rules/branches/{_MAIN}"
_PROT_EP = f"branches/{_MAIN}/protection"

# 各態的 gh 回應（rc, out）
_RULES_PROTECTED = (0, json.dumps([{"type": "pull_request"}, {"type": "required_status_checks"}]))
_RULES_EMPTY = (0, "[]")
_PROT_200 = (0, json.dumps({"required_pull_request_reviews": {}}))
_PROT_404 = (1, "gh: Not Found (HTTP 404)")
_HTTP_403 = (1, "gh: Resource not accessible by integration (HTTP 403)")
_TIMEOUT = (-1, "(逾時 60s)")
_NETERR = (1, "gh: dial tcp: connection refused")


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
    monkeypatch.setattr(config, "AUTOPILOT_REPO", _REPO)
    monkeypatch.setattr(config, "AUTOPILOT_BRANCH", _MAIN)
    monkeypatch.setattr(config, "AUTOPILOT_DRYRUN", False)
    monkeypatch.setattr(config, "AUTOPILOT_FORCE_PUSH", False)
    monkeypatch.setattr(config, "AUTOPILOT_PROTECTION_CHECK", True)
    # owner allowlist 護欄：放行本檔測試用的 owner
    monkeypatch.setattr(config, "PUBLISH_OWNER_ALLOWLIST", frozenset({"octo"}))


async def _check(monkeypatch, overrides):
    spy = RunSpy(overrides)
    monkeypatch.setattr(autopilot, "_run", spy)
    state, detail = await autopilot._check_branch_protection("/clone", _MAIN)
    return state, detail, spy


async def _gate(monkeypatch, overrides):
    """跑完整 _commit_push_merge，回 (ok, msg, spy)。預設有變更、遠端不存在同名分支。"""
    base = {"rev-list --count": (0, "1"), "ls-remote --heads": (0, ""), "pr view": (0, "7")}
    base.update(overrides)
    spy = RunSpy(base)
    monkeypatch.setattr(autopilot, "_run", spy)
    ok, msg = await autopilot._commit_push_merge("/clone", _TASK)
    return ok, msg, spy


# ========================================================================
# 函式層：四態辨識矩陣
# ========================================================================


# --- 態 1：受保護 -------------------------------------------------------
@pytest.mark.asyncio
async def test_state_protected_via_rulesets(monkeypatch):
    """Rulesets 端點回非空陣列 → protected（且不需再打舊端點）。"""
    state, detail, spy = await _check(monkeypatch, {_RULES_EP: _RULES_PROTECTED})
    assert state == "protected", detail
    assert not spy.called(_PROT_EP), "Rulesets 已判定 protected，不應再打舊端點"


@pytest.mark.asyncio
async def test_state_protected_via_legacy_protection(monkeypatch):
    """Rulesets 空但舊 protection 端點回 200 → protected（涵蓋傳統保護設定）。"""
    state, detail, _ = await _check(monkeypatch, {_RULES_EP: _RULES_EMPTY, _PROT_EP: _PROT_200})
    assert state == "protected", detail


# --- 態 2：明確無保護（空陣列 + 404）-----------------------------------
@pytest.mark.asyncio
async def test_state_unprotected_empty_rules_and_404(monkeypatch):
    """Rulesets 乾淨空陣列 + 舊端點 404 → unprotected（雙重確認無保護）。"""
    state, detail, _ = await _check(monkeypatch, {_RULES_EP: _RULES_EMPTY, _PROT_EP: _PROT_404})
    assert state == "unprotected", detail


# --- 態 3：403 無權限 → unknown ----------------------------------------
@pytest.mark.asyncio
async def test_state_unknown_rules_403(monkeypatch):
    """主端點 403（無 Administration:read）→ unknown。"""
    state, detail, _ = await _check(monkeypatch, {_RULES_EP: _HTTP_403})
    assert state == "unknown", detail


@pytest.mark.asyncio
async def test_state_unknown_legacy_403(monkeypatch):
    """主端點空、舊端點 403 → unknown（傳統保護無法確認）。"""
    state, detail, _ = await _check(monkeypatch, {_RULES_EP: _RULES_EMPTY, _PROT_EP: _HTTP_403})
    assert state == "unknown", detail


# --- 態 4：網路失敗／逾時 → unknown ------------------------------------
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "label,resp",
    [("逾時rc=-1", _TIMEOUT), ("連線錯誤", _NETERR)],
    ids=["逾時rc=-1", "連線錯誤"],
)
async def test_state_unknown_network_failure(monkeypatch, label, resp):
    """主端點逾時/連線失敗 → unknown（真實網路失敗時兩端點皆不可達）。"""
    state, detail, _ = await _check(monkeypatch, {_RULES_EP: resp, _PROT_EP: resp})
    assert state == "unknown", f"[{label}] {detail}"


# ========================================================================
# 函式層：端點與目標分支正確性（驗收 #1）
# ========================================================================


@pytest.mark.asyncio
async def test_targets_main_branch_and_rulesets_first(monkeypatch):
    """查的是合併目標 main（非 task 分支），且 Rulesets 端點先於舊端點被呼叫。"""
    state, _, spy = await _check(monkeypatch, {_RULES_EP: _RULES_EMPTY, _PROT_EP: _PROT_404})
    # 端點都針對 main
    assert spy.called(f"repos/{_REPO}/{_RULES_EP}")
    assert spy.called(f"repos/{_REPO}/{_PROT_EP}")
    # 絕不查 task 分支
    assert not spy.called(f"rules/branches/{_TASK_BRANCH}")
    assert not spy.called(f"branches/{_TASK_BRANCH}/protection")
    # Rulesets 優先
    assert spy.index_of(_RULES_EP) < spy.index_of(_PROT_EP)


# ========================================================================
# 接點層：放行 / 中止 / dryrun / 旗標跳過（驗收 #2/#4）
# ========================================================================


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "label,overrides",
    [
        ("protected→放行", {_RULES_EP: _RULES_PROTECTED}),
        ("unprotected→放行", {_RULES_EP: _RULES_EMPTY, _PROT_EP: _PROT_404}),
    ],
    ids=["protected", "unprotected"],
)
async def test_gate_allows_protected_and_unprotected(monkeypatch, label, overrides):
    """protected/unprotected 皆放行，流程走到合併段（開 PR→publisher._merge_flow）。"""
    ok, msg, spy = await _gate(monkeypatch, overrides)
    assert ok is True, f"[{label}] 應放行：{msg}"
    # 合併不再盲合 `gh pr merge`，改以 `pr create`（開 PR）為合併階段接點，再交 _merge_flow 等 CI。
    assert spy.called("pr create"), f"[{label}] 應走到合併段（開 PR）"
    assert not spy.called("pr merge"), f"[{label}] 不該再盲合 gh pr merge"


@pytest.mark.asyncio
async def test_gate_aborts_on_unknown(monkeypatch):
    """unknown → 中止、不 push、不 merge，訊息含『無法確認保護狀態』。"""
    ok, msg, spy = await _gate(monkeypatch, {_RULES_EP: _HTTP_403, _PROT_EP: _HTTP_403})
    assert ok is False
    assert "無法確認保護狀態" in msg, msg
    assert not spy.called("push"), f"unknown 中止不應 push：{spy.calls}"
    assert not spy.called("pr merge")


@pytest.mark.asyncio
async def test_gate_checks_after_lsremote_before_push(monkeypatch):
    """接點順序：保護檢查在 ls-remote 之後、push 之前（unknown 時尚未 push）。"""
    ok, msg, spy = await _gate(monkeypatch, {_RULES_EP: _RULES_EMPTY, _PROT_EP: _PROT_404})
    assert ok is True
    assert spy.index_of("ls-remote") < spy.index_of(_RULES_EP) < spy.index_of("push")


@pytest.mark.asyncio
async def test_dryrun_does_not_call_protection_api(monkeypatch):
    """dryrun 早回，不打任何保護端點 API。"""
    monkeypatch.setattr(config, "AUTOPILOT_DRYRUN", True)
    ok, msg, spy = await _gate(monkeypatch, {_RULES_EP: _RULES_PROTECTED})
    assert ok is True and "[dryrun]" in msg
    assert not spy.called(_RULES_EP), "dryrun 不應打 Rulesets 端點"
    assert not spy.called(_PROT_EP), "dryrun 不應打舊 protection 端點"


@pytest.mark.asyncio
async def test_protection_check_disabled_skips_entirely(monkeypatch):
    """TI_AUTOPILOT_PROTECTION_CHECK=0：整段跳過，即使會 unknown 也放行、不打 API。"""
    monkeypatch.setattr(config, "AUTOPILOT_PROTECTION_CHECK", False)
    ok, msg, spy = await _gate(monkeypatch, {_RULES_EP: _HTTP_403, _PROT_EP: _HTTP_403})
    assert ok is True, f"關閉檢查應放行：{msg}"
    assert not spy.called(_RULES_EP), "關閉檢查不應打 API"
