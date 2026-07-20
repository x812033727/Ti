"""部署黑盒驗證(第 4 階 B1):liveness 之外驗 API 契約;失敗→回滾+page 推播。

守護不變量:
- blackbox_verify:每條探針要 200 且 body 含契約子字串(空=只驗 200);任一失敗即紅,
  訊息帶失敗的 path;base 由 AUTOPILOT_HEALTH_URL 推導。
- redeploy 佈線:TI_DEPLOY_VERIFY=0(預設)不跑探針(零行為變更);=1 且探針紅 →
  rollback + notify deploy_verify_failed(帶 rollback_ok);健檢紅的既有回滾路徑
  也升級為必推播——回滾不得再靜默。
"""

from __future__ import annotations

import pytest

from studio import autonomy, config, deploy


def _fake_curl(monkeypatch, responses):
    """responses: {path 子字串: (rc, body, code)};未列=200 空 body。"""
    calls = []

    async def fake_run(cmd, cwd=None, timeout=600):
        url = cmd[-1]
        calls.append(url)
        for frag, (rc, body, code) in responses.items():
            if frag in url:
                return rc, f"{body}\n{code}"
        return 0, "\n200"

    monkeypatch.setattr(deploy, "_run", fake_run)
    return calls


@pytest.mark.asyncio
async def test_blackbox_pass(monkeypatch):
    _fake_curl(
        monkeypatch,
        {
            "/api/health": (0, '{"ok": true}', "200"),
            "/api/auth/status": (0, '{"auth_enabled": true, "authed": false}', "200"),
        },
    )
    ok, msg = await deploy.blackbox_verify("http://x/api/health")
    assert ok, msg


@pytest.mark.asyncio
async def test_blackbox_fails_on_non200_or_missing_contract(monkeypatch):
    _fake_curl(monkeypatch, {"/api/health": (0, '{"ok": true}', "500")})
    ok, msg = await deploy.blackbox_verify("http://x/api/health")
    assert not ok and "/api/health" in msg

    _fake_curl(monkeypatch, {"/api/health": (0, "<html>殼</html>", "200")})  # 200 但契約缺
    ok, msg = await deploy.blackbox_verify("http://x/api/health")
    assert not ok and "/api/health" in msg


@pytest.mark.asyncio
async def test_redeploy_wiring_verify_fail_rolls_back_and_pages(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "AUTOPILOT_DRYRUN", False)
    monkeypatch.setattr(config, "AUTOPILOT_STATE_DIR", tmp_path)
    monkeypatch.setattr(config, "DEPLOY_VERIFY", True)

    async def git_ok(cmd, cwd=None, timeout=600):
        return 0, ""

    async def head(_dir):
        return "aaaa1111"

    async def reinstall_ok(d, s):
        return True, "restarted"

    async def health_ok(url=None, attempts=12, delay=3):
        return True, "200"

    async def blackbox_fail(base=None):
        return False, "黑盒探針失敗:/api/health(HTTP 500)"

    rolled = {}

    async def fake_rollback(last_good):
        rolled["to"] = last_good
        return True, "回滾完成"

    sent = []
    monkeypatch.setattr(deploy, "_run", git_ok)
    monkeypatch.setattr(deploy, "current_head", head)
    monkeypatch.setattr(deploy, "_reinstall_and_restart", reinstall_ok)
    monkeypatch.setattr(deploy, "health_check", health_ok)
    monkeypatch.setattr(deploy, "blackbox_verify", blackbox_fail)
    monkeypatch.setattr(deploy, "rollback", fake_rollback)
    monkeypatch.setattr(deploy.notify, "send_bg", lambda kind, title, **kw: sent.append((kind, kw)))

    ok, msg = await deploy.redeploy()
    assert not ok and "黑盒探針失敗" in msg
    assert rolled["to"] == "aaaa1111", "驗證紅必回滾"
    assert [k for k, _ in sent] == ["deploy_verify_failed", "rollback_result"], "回滾必推播"
    assert sent[0][1]["rollback_ok"] is True


@pytest.mark.asyncio
async def test_redeploy_verify_disabled_by_default(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "AUTOPILOT_DRYRUN", False)
    monkeypatch.setattr(config, "AUTOPILOT_STATE_DIR", tmp_path)
    monkeypatch.setattr(config, "DEPLOY_VERIFY", False)

    async def git_ok(cmd, cwd=None, timeout=600):
        return 0, ""

    async def head(_dir):
        return "aaaa1111"

    async def reinstall_ok(d, s):
        return True, "restarted"

    async def health_ok(url=None, attempts=12, delay=3):
        return True, "200"

    probes = {"n": 0}

    async def blackbox_spy(base=None):
        probes["n"] += 1
        return False, "不該被呼叫"

    monkeypatch.setattr(deploy, "_run", git_ok)
    monkeypatch.setattr(deploy, "current_head", head)
    monkeypatch.setattr(deploy, "_reinstall_and_restart", reinstall_ok)
    monkeypatch.setattr(deploy, "health_check", health_ok)
    monkeypatch.setattr(deploy, "blackbox_verify", blackbox_spy)

    ok, msg = await deploy.redeploy()
    assert ok and probes["n"] == 0, "旗標關=探針零呼叫(既有行為不變)"


@pytest.mark.asyncio
async def test_governed_redeploy_blocks_when_remote_base_moved_after_approval(
    monkeypatch, tmp_path
):
    pinned = "a" * 40
    approved_merge = "b" * 40
    moved_remote = "c" * 40
    monkeypatch.setattr(config, "AUTOPILOT_DRYRUN", False)
    monkeypatch.setattr(config, "AUTOPILOT_STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(config, "AUTOPILOT_DEPLOY_DIR", tmp_path / "deploy")
    autonomy.ensure_policy(autonomy.CORE_PROJECT_ID)
    autonomy.save_policy(autonomy.CORE_PROJECT_ID, {"mode": "canary", "stage": 3})

    calls = []

    async def fake_run(cmd, cwd=None, timeout=600):
        calls.append(cmd)
        if cmd[:3] == ["git", "fetch", "origin"]:
            return 0, ""
        if cmd == ["git", "rev-parse", f"origin/{config.AUTOPILOT_BRANCH}"]:
            return 0, moved_remote + "\n"
        raise AssertionError(f"unexpected external command after drift: {cmd}")

    async def head(_dir):
        return pinned

    reinstalled = []

    async def reinstall(*args):
        reinstalled.append(args)
        return True, "should not run"

    diff_sha = "d" * 64
    evidence_sha = "e" * 64
    approvals = [
        {
            "provider": provider,
            "verdict": "approve",
            "rationale": "bounded and reversible",
            "diff_sha": diff_sha,
            "evidence_sha": evidence_sha,
        }
        for provider in ("claude", "codex")
    ]
    governance = {
        "risk": "high-reversible",
        "diff_sha": diff_sha,
        "evidence_sha": evidence_sha,
        "rollback": {
            "dry_run": True,
            "backup": True,
            "verified": True,
            "scope_limit": "single deploy",
        },
        "approval_verdicts": approvals,
        "source_sha": pinned,
        "expected_source_sha": approved_merge,
        "run_id": "deploy-drift",
        "task_id": 11,
    }
    monkeypatch.setattr(deploy, "_run", fake_run)
    monkeypatch.setattr(deploy, "current_head", head)
    monkeypatch.setattr(deploy, "_reinstall_and_restart", reinstall)
    monkeypatch.setattr(deploy.notify, "send_bg", lambda *args, **kwargs: None)

    ok, detail = await deploy.redeploy(governance=governance)

    assert ok is False and "source_sha_drift" in detail
    assert ["git", "reset", "--hard", f"origin/{config.AUTOPILOT_BRANCH}"] not in calls
    assert reinstalled == []
    assert autonomy.brake_status()["global"]["active"] is True
    event = next(
        event
        for event in autonomy.read_events(1)
        if event.get("outcome") == "deploy_baseline_blocked"
    )
    assert event["payload"]["expected_source_sha"] == approved_merge
