"""QA coverage for task #3 senior PR-head review closure.

The task-specific acceptance hinges on the senior reviewer seeing the same
anchor-head constraints as QA: confirm the PR head before review, read the diff,
and put the approval marker in the spoken body with that same SHA.
"""

from __future__ import annotations

from studio.orchestrator import StudioSession


async def _noop(_ev):
    pass


ANCHOR_SHA = "eedb86145be1d900720226cc77e6084bff390cd7"


def test_senior_review_prompt_carries_pr_head_closure_constraints():
    session = StudioSession("t", _noop)
    task = {
        "id": 3,
        "title": "senior 對同一 PR head 實讀 diff 審查，發言正文逐字回報決議",
    }
    pm_plan = f"""
PR：#647
PR head sha：`{ANCHOR_SHA}`

給 #2/#3 的硬約束：審查前先 `gh pr view --json headRefOid` 確認 head，
marker 必標該 head sha；若 head 在審查中漂移，兩人須對新 head 重讀重報。

senior 閉環：senior 在發言正文對同一 PR head `{ANCHOR_SHA}` 逐字輸出 `決議: 核可`。
"""

    prompt = session._review_prompt("senior", "senior_approved", task, pm_plan)

    assert ANCHOR_SHA in prompt
    assert "gh pr view --json headRefOid" in prompt
    assert "實讀 diff" in prompt
    assert "發言正文" in prompt
    assert "決議: 核可" in prompt
