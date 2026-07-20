"""QA 驗收：任務 #1「查詢 AUTOPILOT_BRANCH 分支保護狀態的輔助函式」三態專測。

驗證 autopilot._check_branch_protection 的四態判讀：
  - protected   ：Rulesets 非空 list，或舊 protection 端點回 200。
  - unprotected ：Rulesets 回空陣列 [] 且舊 protection 端點 HTTP 404（明確無保護）。
  - unknown     ：HTTP 403（無權限）／網路失敗／逾時（rc=-1）/其他未知組合。

對應任務驗收標準：
  #1 端點正確：優先打 rules/branches/{branch}，舊 branches/{branch}/protection 為輔；
     檢查目標為 config.AUTOPILOT_BRANCH（main，合併目標）而非 task 分支。
  #2 三態明確：404→無保護、403/網路/逾時→不確定。
  #3 fail-safe：不確定狀態絕不誤判為 unprotected（放行）。

手法：攔截 autopilot._run，依指令關鍵片段回傳 (rc, out)，並擷取整個呼叫序列。
全程不發起真實 gh/網路操作。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from studio import autopilot, config

_REPO = "octo/Ti"
_BRANCH = "main"


class RunSpy:
    """攔截 autopilot._run：依指令關鍵片段回傳 (rc, out)，並記錄呼叫序列。"""

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


@pytest.fixture(autouse=True)
def _forbid_real_subprocess(monkeypatch):
    async def _boom(*args, **kwargs):
        raise RuntimeError("test forbids spawning a real subprocess (no network)")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _boom)


@pytest.fixture(autouse=True)
def _base_config(monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_REPO", _REPO)
    monkeypatch.setattr(config, "AUTOPILOT_BRANCH", _BRANCH)
    # owner allowlist 護欄：放行本檔測試用的 owner
    monkeypatch.setattr(config, "PUBLISH_OWNER_ALLOWLIST", frozenset({"octo"}))


async def _check(monkeypatch, overrides):
    spy = RunSpy(overrides)
    monkeypatch.setattr(autopilot, "_run", spy)
    state, detail = await autopilot._check_branch_protection("/clone", _BRANCH)
    return state, detail, spy


# === 態 1：有保護（Rulesets 非空）=========================================


@pytest.mark.asyncio
async def test_protected_via_rulesets(monkeypatch):
    rules = json.dumps([{"type": "pull_request"}, {"type": "required_status_checks"}])
    state, detail, spy = await _check(monkeypatch, {"rules/branches": (0, rules)})
    assert state == "protected", detail
    # 端點正確：主端點為 rules/branches/{main}
    assert spy.called(f"repos/{_REPO}/rules/branches/{_BRANCH}")
    # 非空即受保護，無需再打舊端點
    assert not spy.called("/protection"), "Rulesets 已判 protected 不應再查舊端點"


@pytest.mark.asyncio
async def test_protected_via_legacy_protection(monkeypatch):
    """Rulesets 空陣列但傳統 branch-protection 存在（200）→ 仍視為 protected。"""
    state, detail, spy = await _check(
        monkeypatch,
        {"rules/branches": (0, "[]"), "/protection": (0, '{"required_status_checks":{}}')},
    )
    assert state == "protected", detail
    assert spy.called(f"repos/{_REPO}/branches/{_BRANCH}/protection")


# === 態 2：明確無保護（Rulesets 空 + 舊端點 404）==========================


@pytest.mark.asyncio
async def test_unprotected_empty_rules_and_404(monkeypatch):
    state, detail, spy = await _check(
        monkeypatch,
        {
            "rules/branches": (0, "[]"),
            "/protection": (1, "gh: Not Found (HTTP 404)"),
        },
    )
    assert state == "unprotected", detail
    # 必須兩端點都查過才敢下「無保護」結論
    assert spy.called("rules/branches") and spy.called("/protection")


@pytest.mark.asyncio
async def test_rules_404_is_uncertain_not_unprotected(monkeypatch):
    """Rulesets 端點本身 404 屬「未乾淨確認」（該端點正常回空陣列而非 404）→ 即使舊端點
    亦 404 也不得放行，落 unknown（fail-safe：絕不憑異常主端點 + 舊端點 404 誤判無保護）。"""
    state, detail, _ = await _check(
        monkeypatch,
        {
            "rules/branches": (1, "gh: Not Found (HTTP 404)"),
            "/protection": (1, "gh: Not Found (HTTP 404)"),
        },
    )
    assert state == "unknown", detail
    assert state != "unprotected"


# === 態 3：403 無權限 → unknown（fail-safe，絕不誤判放行）=================


@pytest.mark.asyncio
async def test_unknown_on_403_rulesets(monkeypatch):
    state, detail, spy = await _check(
        monkeypatch,
        {"rules/branches": (1, "gh: Resource not accessible by integration (HTTP 403)")},
    )
    assert state == "unknown", detail
    assert state != "unprotected", "403 絕不可誤判為無保護（放行）"
    # 主端點 403 即直接判 unknown，不必續查
    assert not spy.called("/protection")


@pytest.mark.asyncio
async def test_unknown_on_403_legacy(monkeypatch):
    """Rulesets 404（續查），舊端點回 403 → unknown。"""
    state, detail, _ = await _check(
        monkeypatch,
        {
            "rules/branches": (1, "gh: Not Found (HTTP 404)"),
            "/protection": (1, "gh: Resource not accessible (HTTP 403)"),
        },
    )
    assert state == "unknown", detail
    assert state != "unprotected"


# === 態 4：網路失敗 / 逾時 → unknown ======================================


@pytest.mark.asyncio
async def test_unknown_on_timeout_rulesets(monkeypatch):
    # _run 逾時回 (-1, "(逾時 60s)")
    state, detail, _ = await _check(monkeypatch, {"rules/branches": (-1, "(逾時 60s)")})
    assert state == "unknown", detail
    assert state != "unprotected"


@pytest.mark.asyncio
async def test_unknown_on_network_failure_legacy(monkeypatch):
    """Rulesets 404 續查，舊端點網路失敗（rc=-1 逾時）→ unknown。"""
    state, detail, _ = await _check(
        monkeypatch,
        {
            "rules/branches": (1, "gh: Not Found (HTTP 404)"),
            "/protection": (-1, "(逾時 60s)"),
        },
    )
    assert state == "unknown", detail
    assert state != "unprotected"


# === 兜底：未知組合一律落 unknown（保守）=================================


@pytest.mark.asyncio
async def test_unknown_default_on_weird_combo(monkeypatch):
    """Rulesets 空陣列但舊端點回非 404/403/逾時的怪 rc → 不敢判無保護，落 unknown。"""
    state, detail, _ = await _check(
        monkeypatch,
        {
            "rules/branches": (0, "[]"),
            "/protection": (1, "gh: some unexpected error (HTTP 500)"),
        },
    )
    assert state == "unknown", detail
    assert state != "unprotected", "未知錯誤絕不可當無保護放行"


@pytest.mark.asyncio
async def test_unknown_on_invalid_json(monkeypatch):
    """Rulesets rc==0 但回非 JSON / 非 list → rulesets 未乾淨確認，不據此判 protected；
    此時即使舊端點 404 也不得放行，落 unknown（fail-safe，絕不 fall-through 當無保護）。"""
    state, detail, _ = await _check(
        monkeypatch,
        {
            "rules/branches": (0, "not-json-garbage"),
            "/protection": (1, "gh: Not Found (HTTP 404)"),
        },
    )
    assert state == "unknown", detail
    assert state != "unprotected"


# === 端點/目標分支正確性 ================================================


@pytest.mark.asyncio
async def test_targets_merge_base_branch_not_task_branch(monkeypatch):
    """檢查目標必須是合併目標分支（傳入的 branch=main），端點路徑正確。"""
    rules = json.dumps([{"type": "pull_request"}])
    _, _, spy = await _check(monkeypatch, {"rules/branches": (0, rules)})
    # 主端點：rules/branches/{branch}；確認 branch 是 main 而非 autopilot/task-*
    assert spy.called(f"repos/{_REPO}/rules/branches/{_BRANCH}")
    for c in spy.calls:
        joined = " ".join(c)
        assert "autopilot/task-" not in joined, "絕不可查 task 分支的保護狀態"
