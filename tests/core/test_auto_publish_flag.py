"""單元測試：StudioSession 的 `auto_publish` 開關（Option 2 單一發佈者）。

設計決策（2026-06-21）：autopilot 成為唯一發佈者，它跑的 session 必須 NOT 自行發佈，
否則同一份成果會被 session（ti-studio/<sid>）與 autopilot（autopilot/task-N）各開一個 PR。

驗證：
1. auto_publish=False → _maybe_publish / _maybe_publish_inner 直接 return，完全不呼叫 publisher.publish。
2. auto_publish 預設為 True 且在 PUBLISH_AUTO+is_configured 條件成立時照常會呼叫 publisher.publish
   （確認新加的早退不誤傷正常發佈路徑）。
"""

from __future__ import annotations

import pytest

from studio import config, orchestrator, publisher, runner
from studio.orchestrator import StudioSession


async def _bc(_ev):
    return None


def _install_publish_spy(monkeypatch):
    """記錄 publisher.publish 呼叫次數；回傳 calls list。一律不真的發佈。"""
    calls: list = []

    async def _fake_publish(*args, **kwargs):
        calls.append((args, kwargs))
        # 回一個「未 push、無 PR」的最小結果即可（測試不進 CI 迴圈）
        return publisher.PublishResult(False, "test stub")

    monkeypatch.setattr(publisher, "publish", _fake_publish)
    return calls


@pytest.mark.asyncio
async def test_auto_publish_false_skips_publish(monkeypatch, tmp_path):
    """auto_publish=False：即使 PUBLISH_AUTO 開啟、shippable，也不呼叫 publisher.publish。"""
    monkeypatch.setattr(config, "PUBLISH_AUTO", True)
    monkeypatch.setattr(publisher, "is_configured", lambda: True)
    calls = _install_publish_spy(monkeypatch)

    session = StudioSession("t", _bc, experts={}, cwd=tmp_path, auto_publish=False)
    session._requirement = "做一個東西"
    await session._maybe_publish(True)  # shippable=True

    assert calls == [], "auto_publish=False 不該呼叫 publisher.publish"


@pytest.mark.asyncio
async def test_auto_publish_false_inner_also_skips(monkeypatch, tmp_path):
    """直接打 _maybe_publish_inner 也須早退（早退置於既有早退序列中，與外層 contextvar 無關）。"""
    monkeypatch.setattr(config, "PUBLISH_AUTO", True)
    monkeypatch.setattr(publisher, "is_configured", lambda: True)
    calls = _install_publish_spy(monkeypatch)

    session = StudioSession("t", _bc, experts={}, cwd=tmp_path, auto_publish=False)
    session._requirement = "做一個東西"
    await session._maybe_publish_inner(True)

    assert calls == []


@pytest.mark.asyncio
async def test_auto_publish_default_true_publishes(monkeypatch, tmp_path):
    """預設 auto_publish=True：條件成立時照常呼叫 publisher.publish（早退不誤傷正常路徑）。"""
    monkeypatch.setattr(config, "PUBLISH_AUTO", True)
    monkeypatch.setattr(config, "PUBLISH_MERGE", False)  # 不進 CI/合併迴圈
    monkeypatch.setattr(publisher, "is_configured", lambda: True)
    calls = _install_publish_spy(monkeypatch)

    session = StudioSession("t", _bc, experts={}, cwd=tmp_path)  # 預設 auto_publish=True
    session._requirement = "做一個東西"
    await session._maybe_publish(True)

    assert len(calls) == 1, "預設 auto_publish=True 且條件成立時應呼叫一次 publisher.publish"


@pytest.mark.asyncio
async def test_publish_guard_blocks_before_any_external_publish(monkeypatch, tmp_path):
    """guard 拒絕時要留下結構化結果，且 publisher 必須零呼叫。"""
    monkeypatch.setattr(config, "PUBLISH_AUTO", True)
    monkeypatch.setattr(publisher, "is_configured", lambda: True)
    calls = _install_publish_spy(monkeypatch)
    guarded = []

    async def guard(attempt, evidence):
        guarded.append((attempt, evidence))
        return False, "shadow only"

    session = StudioSession("t", _bc, experts={}, cwd=tmp_path, publish_guard=guard)
    session._requirement = "做一個東西"
    session._shippable = True
    await session._maybe_publish(True)

    assert calls == []
    assert guarded[0][0] == "initial"
    assert guarded[0][1]["shippable"] is True
    assert session._publish_result["ok"] is False
    assert session._publish_result["detail"] == "shadow only"


@pytest.mark.asyncio
async def test_ci_repush_rechecks_guard_and_blocks_stale_approval(monkeypatch, tmp_path):
    """CI 修正會改變 diff，重推前必須再跑一次 guard，不得沿用首輪核可。"""
    monkeypatch.setattr(config, "PUBLISH_AUTO", True)
    monkeypatch.setattr(config, "PUBLISH_MERGE", True)
    monkeypatch.setattr(config, "PUBLISH_CI_MAX_ROUNDS", 2)
    monkeypatch.setattr(publisher, "is_configured", lambda: True)
    attempts = []
    repushes = []

    async def guard(attempt, evidence):
        attempts.append(attempt)
        return (attempt == "initial"), "changed diff requires a fresh approval"

    async def fake_publish(*args, **kwargs):
        return publisher.PublishResult(
            True,
            "CI failed",
            branch="ti-studio/t",
            repo="owner/repo",
            pushed=True,
            pr_number=7,
            outcome=publisher.MergeOutcome.CI_FAILED,
        )

    async def fake_logs(*args, **kwargs):
        return "test failed"

    async def fake_repush(*args, **kwargs):
        repushes.append(1)
        return runner.RunOutput("git push", 0, "", False)

    class Engineer:
        async def speak(self, prompt, broadcast):
            return "fixed"

    monkeypatch.setattr(publisher, "publish", fake_publish)
    monkeypatch.setattr(publisher, "ci_failure_logs", fake_logs)
    monkeypatch.setattr(publisher, "repush", fake_repush)

    session = StudioSession("t", _bc, experts={}, cwd=tmp_path, publish_guard=guard)
    session._requirement = "做一個東西"
    session._shippable = True

    async def no_commit(*args, **kwargs):
        return None

    monkeypatch.setattr(session, "_commit", no_commit)
    await session._maybe_publish(True, Engineer())

    assert attempts == ["initial", "ci_repush_1"]
    assert repushes == []
    assert session._publish_result["ok"] is False


@pytest.mark.asyncio
async def test_post_publish_health_failure_is_machine_readable(monkeypatch, tmp_path):
    """合併成功但健康證據紅時，最終 result 不得仍是 ok。"""
    monkeypatch.setattr(config, "PUBLISH_AUTO", True)
    monkeypatch.setattr(config, "PUBLISH_MERGE", True)
    monkeypatch.setattr(publisher, "is_configured", lambda: True)
    sha = "a" * 40

    async def fake_publish(*args, **kwargs):
        return publisher.PublishResult(
            True,
            "merged",
            branch="ti-studio/t",
            repo="owner/repo",
            pushed=True,
            pr_number=7,
            merged=True,
            outcome=publisher.MergeOutcome.MERGED,
            merge_sha=sha,
        )

    verified = []

    async def verifier(result):
        verified.append(result)
        return False, "deployment_health_timeout:revision_mismatch"

    monkeypatch.setattr(publisher, "publish", fake_publish)
    session = StudioSession("t", _bc, experts={}, cwd=tmp_path, post_publish_verifier=verifier)
    session._requirement = "做一個東西"
    await session._maybe_publish(True)

    assert verified[0]["merge_sha"] == sha
    assert session._publish_result["merged"] is True
    assert session._publish_result["health_verified"] is False
    assert session._publish_result["ok"] is False


@pytest.mark.asyncio
async def test_managed_guard_fails_closed_when_publisher_is_unconfigured(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "PUBLISH_AUTO", False)
    guarded = []

    async def guard(attempt, evidence):
        guarded.append(attempt)
        return True, "policy passed"

    session = StudioSession("t", _bc, experts={}, cwd=tmp_path, publish_guard=guard)
    session._requirement = "做一個東西"
    await session._maybe_publish(True)

    assert guarded == ["initial"]
    assert session._publish_result["ok"] is False
    assert "fail-closed" in session._publish_result["detail"]


@pytest.mark.asyncio
async def test_constructor_stores_auto_publish_flag(tmp_path):
    """建構子保存 auto_publish 旗標（預設 True）。"""
    s_default = StudioSession("a", _bc, experts={}, cwd=tmp_path)
    s_off = StudioSession("b", _bc, experts={}, cwd=tmp_path, auto_publish=False)
    assert s_default._auto_publish is True
    assert s_off._auto_publish is False


def test_autopilot_constructs_session_with_auto_publish_false():
    """source-level：autopilot.run_one_task 構造 StudioSession 時傳 auto_publish=False。"""
    import ast
    import inspect

    from studio import autopilot

    src = inspect.getsource(autopilot.run_one_task)
    tree = ast.parse(src)
    found = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id == "StudioSession":
                kws = {kw.arg: kw.value for kw in node.keywords}
                assert "auto_publish" in kws, "run_one_task 須顯式傳 auto_publish"
                v = kws["auto_publish"]
                assert isinstance(v, ast.Constant) and v.value is False
                found = True
    assert found, "未在 run_one_task 找到 StudioSession(...) 構造"


# 觸發 orchestrator import 被使用（避免未用 import 警告），同時健全性檢查符號存在。
def test_orchestrator_has_maybe_publish():
    assert hasattr(orchestrator.StudioSession, "_maybe_publish")
    assert hasattr(orchestrator.StudioSession, "_maybe_publish_inner")
