"""QA 驗收：任務#2 — 四種結局不再糊成一團，外層不再只看 merged=False。

本任務焦點＝『外層可機器判讀結局』的串接鏈路（有別於任務#1 的內部分流）：
  publish() → PublishResult.outcome → to_dict() → events.publish_result
            → orchestrator.broadcast → 前端 OUTCOME_BADGE

逐條對應驗收標準，補強外層傳遞層的覆蓋：
  5. 四種結局皆有對應且可區分的回傳訊息；merged=False 不再是唯一信號。
  + 外層（事件 payload / to_dict）必帶 outcome，且 enum↔前端徽章一致（無漏顯示）。
"""

from __future__ import annotations

import re

import pytest
from _repo import REPO_ROOT

from studio import config, events, orchestrator, publisher, runner
from studio.publisher import MergeOutcome, PublishResult

pytestmark = pytest.mark.asyncio


# === to_dict 契約：外層拿得到可機器判讀的 outcome =============================


async def test_to_dict_exposes_outcome_key_always():
    """to_dict 必含 outcome 鍵（成功/失敗/未嘗試合併三類皆然），外層不必只猜 merged。"""
    # 未嘗試合併（merge=False）→ outcome=None，但鍵仍在
    d0 = PublishResult(True, "已 push").to_dict()
    assert "outcome" in d0 and d0["outcome"] is None
    assert "merged" in d0  # 既有鍵不破壞（向後相容）

    # 已合併
    d1 = PublishResult(True, "ok", merged=True, outcome=MergeOutcome.MERGED).to_dict()
    assert d1["outcome"] == "merged" and d1["merged"] is True


async def test_failure_outcomes_distinguishable_despite_same_merged_false():
    """四種非成功結局 merged 皆為 False，但 outcome 互異 → 外層能區分（核心訴求）。"""
    outcomes = [
        MergeOutcome.CI_FAILED,
        MergeOutcome.BLOCKED,
        MergeOutcome.CONFLICT,
        MergeOutcome.TIMEOUT,
    ]
    dicts = [
        PublishResult(True, f"d-{o.value}", merged=False, outcome=o).to_dict() for o in outcomes
    ]
    # 全部 merged=False（證明舊信號無法區分）
    assert all(d["merged"] is False for d in dicts)
    # 但 outcome 全互異（證明新信號可區分）
    assert len({d["outcome"] for d in dicts}) == len(outcomes)
    # 且每個 outcome 都是非空字串、detail 非空（無 silent failed）
    for d in dicts:
        assert isinstance(d["outcome"], str) and d["outcome"]
        assert d["detail"].strip()


# === 端到端：publish() 各結局 → to_dict 帶可區分 outcome ====================


@pytest.fixture
def _ready(monkeypatch):
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
    return monkeypatch


@pytest.mark.parametrize(
    "outcome,merged_flag",
    [
        (MergeOutcome.MERGED, True),
        (MergeOutcome.CI_FAILED, False),
        (MergeOutcome.BLOCKED, False),
        (MergeOutcome.TIMEOUT, False),
    ],
)
async def test_publish_four_outcomes_reach_to_dict(_ready, outcome, merged_flag):
    async def fake_flow(number, payload, **kw):
        return outcome, f"detail-{outcome.value}"

    _ready.setattr(publisher, "_merge_flow", fake_flow)
    res = await publisher.publish("/tmp", "s1", "需求", merge=True)
    d = res.to_dict()
    assert d["outcome"] == outcome.value
    assert d["merged"] is merged_flag
    assert d["detail"].strip()


# === 外層串接：orchestrator._maybe_publish 廣播事件帶 outcome ===============


def _make_orch(captured):
    async def broadcast(event):
        captured.append(event)

    o = orchestrator.StudioSession("sess-qa", broadcast, cwd="/tmp/ws")
    o._requirement = "需求"
    return o


@pytest.mark.parametrize(
    "outcome",
    [MergeOutcome.MERGED, MergeOutcome.CI_FAILED, MergeOutcome.BLOCKED, MergeOutcome.TIMEOUT],
)
async def test_maybe_publish_broadcasts_outcome_in_event(monkeypatch, outcome):
    """orchestrator 自動發佈時，廣播的 publish_result 事件 payload 必帶 outcome。"""
    monkeypatch.setattr(config, "PUBLISH_AUTO", True)
    monkeypatch.setattr(publisher, "is_configured", lambda: True)

    async def spy_publish(cwd, session_id, requirement, *, merge=False):
        return PublishResult(
            True, f"d-{outcome.value}", merged=(outcome == MergeOutcome.MERGED), outcome=outcome
        )

    monkeypatch.setattr(publisher, "publish", spy_publish)

    captured = []
    await _make_orch(captured)._maybe_publish(done=True)

    pub_events = [e for e in captured if e.type == events.EventType.PUBLISH_RESULT]
    assert len(pub_events) == 1
    payload = pub_events[0].payload
    # 事件 payload 即 to_dict()，外層／前端可機器判讀 outcome
    assert payload["outcome"] == outcome.value
    assert "merged" in payload and "detail" in payload


# === 一致性契約：所有 MergeOutcome 值在前端 OUTCOME_BADGE 都有對應 ==========


def _frontend_badge_keys() -> set[str]:
    app_js = REPO_ROOT / "web" / "app.js"
    text = app_js.read_text(encoding="utf-8")
    m = re.search(r"OUTCOME_BADGE\s*=\s*\{(.*?)\}", text, re.S)
    assert m, "web/app.js 找不到 OUTCOME_BADGE"
    return set(re.findall(r"(\w+)\s*:", m.group(1)))


async def test_every_outcome_has_frontend_badge():
    """每個 MergeOutcome 值都要有前端徽章，避免新結局在 UI 漏顯示（退回模糊狀態）。"""
    badge_keys = _frontend_badge_keys()
    missing = [o.value for o in MergeOutcome if o.value not in badge_keys]
    assert not missing, f"前端 OUTCOME_BADGE 缺少結局徽章：{missing}"


async def test_outcome_label_covers_all_failure_outcomes():
    """後端給人看的 _OUTCOME_LABEL 需涵蓋全部結局（detail 必有可讀標籤，不 silent）。"""
    missing = [o for o in MergeOutcome if o not in publisher._OUTCOME_LABEL]
    assert not missing, f"_OUTCOME_LABEL 缺少：{[o.value for o in missing]}"
