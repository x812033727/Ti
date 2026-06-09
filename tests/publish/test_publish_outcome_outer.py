"""任務 #2：四種／六種結局不再糊成一團，外層（to_dict / 事件 payload）可機器判讀並區分。

驗證：
- publish() 對每種 MergeOutcome 都把 outcome 寫進 PublishResult 並反映在 detail。
- to_dict() 的 outcome 鍵對每種結局是不同字串（不再只看 merged=False）。
- events.publish_result 原樣透傳 outcome，前端據此可區分。
"""

from __future__ import annotations

import pytest

from studio import config, events, publisher, runner
from studio.publisher import MergeOutcome


@pytest.fixture
def _configured(monkeypatch):
    monkeypatch.setattr(config, "GITHUB_TOKEN", "tok")
    monkeypatch.setattr(config, "PUBLISH_REPO", "o/r")
    monkeypatch.setattr(config, "PUBLISH_BASE", "main")

    async def _noop(*a, **k):
        return True

    monkeypatch.setattr(runner, "git_init", _noop)
    monkeypatch.setattr(runner, "git_commit", _noop)

    async def fake_push(cwd, branch, url):
        return runner.RunOutput(command="git push", exit_code=0, output="ok", timed_out=False)

    async def fake_pr(payload):
        return True, "https://github.com/o/r/pull/7"

    monkeypatch.setattr(publisher, "_push", fake_push)
    monkeypatch.setattr(publisher, "_open_pr", fake_pr)


@pytest.mark.parametrize(
    "outcome",
    [
        MergeOutcome.MERGED,
        MergeOutcome.CI_FAILED,
        MergeOutcome.BLOCKED,
        MergeOutcome.CONFLICT,
        MergeOutcome.TIMEOUT,
        MergeOutcome.ERROR,
    ],
)
@pytest.mark.asyncio
async def test_publish_propagates_each_outcome(monkeypatch, _configured, outcome):
    async def fake_flow(number, payload, **kw):
        return outcome, f"detail for {outcome.value}"

    monkeypatch.setattr(publisher, "_merge_flow", fake_flow)
    res = await publisher.publish("/tmp", "s1", "需求", merge=True)

    # 結局寫進 outcome 欄位，且 to_dict 以該字串輸出（外層可機器判讀）
    assert res.outcome == outcome
    assert res.to_dict()["outcome"] == outcome.value
    # 僅 MERGED 才 merged=True；其餘四／五種失敗皆 merged=False 但 outcome 各異
    assert res.merged is (outcome == MergeOutcome.MERGED)
    # 不丟例外、皆有 detail（無 silent failed）
    assert res.detail


@pytest.mark.asyncio
async def test_failed_outcomes_are_distinct(monkeypatch, _configured):
    """四種失敗結局的 to_dict outcome 必須兩兩不同，不再糊成單一 merged=False。"""
    seen = {}
    for oc in (
        MergeOutcome.CI_FAILED,
        MergeOutcome.BLOCKED,
        MergeOutcome.CONFLICT,
        MergeOutcome.TIMEOUT,
    ):

        async def fake_flow(number, payload, _oc=oc, **kw):
            return _oc, "d"

        monkeypatch.setattr(publisher, "_merge_flow", fake_flow)
        res = await publisher.publish("/tmp", "s1", "需求", merge=True)
        seen[oc] = res.to_dict()["outcome"]
    assert len(set(seen.values())) == 4  # 四種結局四個不同字串


def test_event_payload_carries_outcome():
    """events.publish_result 原樣透傳 outcome，前端可據此顯示徽章。"""
    res = publisher.PublishResult(True, "x", outcome=MergeOutcome.BLOCKED)
    ev = events.publish_result("s1", res.to_dict())
    assert ev.to_dict()["payload"]["outcome"] == "blocked"
    assert ev.to_dict()["payload"]["merged"] is False
