"""單元測試：merge 終局失敗訊息的分診標記閉環（完成率第三輪 B-5）。

背景：`_commit_push_merge` 走到「等 CI→合併」失敗時，msg 進 `_handle_gate_failure` 落
backlog note；backlog.triage_failed 只對 note 命中 INFRA_FAILURE_RE 的 failed 自動重排。
舊行為：TIMEOUT 的 detail 恰含「逾時」字樣間接命中，但 ERROR（API rate limit/5xx/網路
例外）完全漏網 → 暫時性 infra 失敗被當實質失敗，達重試上限即永久 failed、14 天才 park。

新行為：outcome 為 TIMEOUT/ERROR 時，msg 尾明確附「unreachable（網路暫時性，可分診重試）」
標記（與 _merge_fail_note 同一字串）；CI_FAILED/CONFLICT/BLOCKED 是實質失敗，刻意不附
（附了會讓 triage 無限重排真失敗）。
"""

from __future__ import annotations

import asyncio

import pytest

from studio import autopilot, backlog, config, publisher

_TASK = {"id": "7", "title": "示範任務", "detail": "細節"}

_HAS_CHANGE = {"rev-list --count": (0, "1"), "--json number": (0, "42")}


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
    monkeypatch.setattr(config, "AUTOPILOT_DRYRUN", False)
    monkeypatch.setattr(config, "AUTOPILOT_FORCE_PUSH", False)
    monkeypatch.setattr(config, "AUTOPILOT_BRANCH", "main")


def _install(monkeypatch, outcome, detail):
    spy = RunSpy({**_HAS_CHANGE})

    async def _flow(number, payload, **kwargs):
        return (outcome, detail)

    monkeypatch.setattr(autopilot, "_run", spy)
    monkeypatch.setattr(publisher, "_merge_flow", _flow)
    return spy


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "detail"),
    [
        (publisher.MergeOutcome.TIMEOUT, "等待 CI 完成中"),
        (publisher.MergeOutcome.ERROR, "API rate limit exceeded"),
    ],
)
async def test_transient_outcomes_get_infra_marker(monkeypatch, outcome, detail):
    """TIMEOUT/ERROR＝暫時性 infra：msg 帶 unreachable 標記，triage 可自動重排。

    detail 刻意不含「逾時/timeout」字樣，驗證標記不依賴 detail 內文碰巧命中。
    """
    spy = _install(monkeypatch, outcome, detail)
    ok, msg = await autopilot._commit_push_merge("/clone", _TASK)

    assert ok is False
    assert "unreachable" in msg
    assert backlog.INFRA_FAILURE_RE.search(msg)
    assert spy.called("pr close"), "終局失敗仍應關 PR 刪分支（既有行為不變）"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome",
    [
        publisher.MergeOutcome.CI_FAILED,
        publisher.MergeOutcome.CONFLICT,
        publisher.MergeOutcome.BLOCKED,
    ],
)
async def test_substantive_outcomes_have_no_infra_marker(monkeypatch, outcome):
    """CI 紅/真衝突/被保護擋下＝實質失敗：不附標記，達重試上限即永久 failed（不無限重排）。"""
    _install(monkeypatch, outcome, "test (3.12) failed")
    ok, msg = await autopilot._commit_push_merge("/clone", _TASK)

    assert ok is False
    assert "unreachable" not in msg
    assert not backlog.INFRA_FAILURE_RE.search(msg)
