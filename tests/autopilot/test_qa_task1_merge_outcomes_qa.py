"""QA 驗收：任務#1 — 消除 PR 自動合併的 silent failed。

逐條對應驗收標準，補強既有測試尚未嚴格涵蓋的關鍵風險點：
  1. 被分支保護擋下時，回報含「原因類別」而非僅原始 HTTP text。
  2. CI 仍在跑→續等；CI 失敗→明確「CI 未過」且不誤判成功、不嘗試合併。
  3. 等待具 timeout，逾時回明確「逾時」結局，不無限阻塞。
  4. stale／409 有有限次數重試＋backoff，超限才放棄。
  5. 四種結局（成功／CI失敗／被擋／逾時）從 publish() 端皆有可區分訊息且寫進 outcome，
     全程不丟例外（無 silent failed）。
  6. timeout／間隔／重試次數可由設定調整。
  + 防「假修」：behind 重試時 update-branch 後須重抓新 head sha 並重等該新 sha 的 CI。
"""

import pytest

from studio import config, publisher, runner
from studio.publisher import MergeOutcome

pytestmark = pytest.mark.asyncio


# --- 共用前置：配置 + push/PR 成功，merge 流程以 stub 注入 -------------------


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


async def _publish_with_flow(monkeypatch, outcome, detail):
    async def fake_flow(number, payload, **kw):
        return outcome, detail

    monkeypatch.setattr(publisher, "_merge_flow", fake_flow)
    return await publisher.publish("/tmp", "s1", "需求", merge=True)


# === 驗收標準 5：四種結局從 publish() 端可區分，且不丟例外 =====================


async def test_publish_outcome_merged(_ready):
    res = await _publish_with_flow(_ready, MergeOutcome.MERGED, "deadbeef")
    assert res.ok and res.merged
    assert res.outcome is MergeOutcome.MERGED
    assert res.to_dict()["outcome"] == "merged"


async def test_publish_outcome_ci_failed(_ready):
    res = await _publish_with_flow(_ready, MergeOutcome.CI_FAILED, "CI 失敗：lint job")
    assert res.ok  # 整體流程不報例外
    assert not res.merged
    assert res.outcome is MergeOutcome.CI_FAILED
    assert res.to_dict()["outcome"] == "ci_failed"
    assert "CI 未過" in res.detail  # 給人看的類別標籤


async def test_publish_outcome_blocked(_ready):
    res = await _publish_with_flow(_ready, MergeOutcome.BLOCKED, "不可合併（405）")
    assert not res.merged
    assert res.outcome is MergeOutcome.BLOCKED
    assert res.to_dict()["outcome"] == "blocked"
    assert "擋下" in res.detail or "保護" in res.detail


async def test_publish_outcome_timeout(_ready):
    res = await _publish_with_flow(_ready, MergeOutcome.TIMEOUT, "等待 CI 逾時（已等待 600s）")
    assert not res.merged
    assert res.outcome is MergeOutcome.TIMEOUT
    assert res.to_dict()["outcome"] == "timeout"
    assert "逾時" in res.detail


async def test_publish_four_outcomes_are_distinct(_ready):
    """四種結局的 (outcome, detail) 互不相同 → 可區分，杜絕糊成一團。"""
    cases = [
        (MergeOutcome.MERGED, "deadbeef"),
        (MergeOutcome.CI_FAILED, "CI 失敗"),
        (MergeOutcome.BLOCKED, "405 受保護"),
        (MergeOutcome.TIMEOUT, "等待 CI 逾時"),
    ]
    seen_outcome = set()
    seen_detail = set()
    for oc, dt in cases:
        res = await _publish_with_flow(_ready, oc, dt)
        seen_outcome.add(res.to_dict()["outcome"])
        seen_detail.add(res.detail)
    assert len(seen_outcome) == 4
    assert len(seen_detail) == 4


async def test_publish_no_silent_failure_outcome_always_set(_ready):
    """任何失敗結局都必須有 outcome 與非空 detail（不得 silent failed）。"""
    for oc in (
        MergeOutcome.CI_FAILED,
        MergeOutcome.BLOCKED,
        MergeOutcome.CONFLICT,
        MergeOutcome.TIMEOUT,
        MergeOutcome.ERROR,
    ):
        res = await _publish_with_flow(_ready, oc, f"detail-{oc.value}")
        assert res.outcome is oc
        assert res.to_dict()["outcome"] is not None
        assert res.detail.strip()


# === 驗收標準 1：被擋下時 detail 帶「原因類別」，非僅原始 HTTP text ============


@pytest.fixture
def _flow_stub(monkeypatch):
    """_merge_flow 內部各 IO 函式的可調 stub。"""
    st = {
        "pr": {"head": {"sha": "sha1"}, "mergeable": True, "mergeable_state": "clean"},
        "ci": ("pass", "ok"),
        "merge_seq": [(MergeOutcome.MERGED, "deadbeef", False)],
        "merge_calls": 0,
        "update_calls": 0,
        "waited_shas": [],
    }

    async def fake_status(number, **kw):
        return st["pr"]

    async def fake_wait(sha, **kw):
        st["waited_shas"].append(sha)
        return st["ci"]

    async def fake_merge(number, payload):
        i = min(st["merge_calls"], len(st["merge_seq"]) - 1)
        st["merge_calls"] += 1
        return st["merge_seq"][i]

    async def fake_update(number):
        st["update_calls"] += 1
        # 模擬 update-branch 改變 head sha（產生新 commit）
        st["pr"] = {**st["pr"], "head": {"sha": f"sha{st['update_calls'] + 1}"}}
        return True

    async def fake_sleep(_):
        return None

    monkeypatch.setattr(publisher, "_get_pr_status", fake_status)
    monkeypatch.setattr(publisher, "_wait_for_ci", fake_wait)
    monkeypatch.setattr(publisher, "_merge_pr", fake_merge)
    monkeypatch.setattr(publisher, "_update_branch", fake_update)
    st["sleep"] = fake_sleep
    return st


async def _run_flow(st, retries=3, ci_timeout=60, ci_interval=1):
    return await publisher._merge_flow(
        7, {}, ci_timeout=ci_timeout, ci_interval=ci_interval, retries=retries, sleep=st["sleep"]
    )


async def test_blocked_detail_names_cause_category_not_raw_text(_flow_stub):
    """CI 已過卻被擋（mergeable_state=blocked）→ 回報含結構化類別線索，而非只丟 405 文字。"""
    _flow_stub["pr"] = {"head": {"sha": "s"}, "mergeable": True, "mergeable_state": "blocked"}
    _flow_stub["merge_seq"] = [
        (MergeOutcome.BLOCKED, "不可合併／受保護（405）：raw github text", False)
    ]
    outcome, detail = await _run_flow(_flow_stub)
    assert outcome is MergeOutcome.BLOCKED
    # 不再只回原始 text：detail 帶上結構化 mergeable_state 供分辨缺審核/規則
    assert "mergeable_state=blocked" in detail


async def test_dirty_reclassified_as_conflict(_flow_stub):
    """粗分類 405 但結構化狀態 dirty → 精準改判為 CONFLICT（衝突），與 blocked 區分。"""
    _flow_stub["pr"] = {"head": {"sha": "s"}, "mergeable": False, "mergeable_state": "dirty"}
    _flow_stub["merge_seq"] = [(MergeOutcome.BLOCKED, "不可合併（405）", False)]
    outcome, detail = await _run_flow(_flow_stub)
    assert outcome is MergeOutcome.CONFLICT
    assert "mergeable_state=dirty" in detail


async def test_blocked_405_does_not_enter_retry_loop(_flow_stub):
    """405（不可重試）只嘗試一次合併，不白等重試。"""
    _flow_stub["pr"] = {"head": {"sha": "s"}, "mergeable": True, "mergeable_state": "blocked"}
    _flow_stub["merge_seq"] = [(MergeOutcome.BLOCKED, "405 受保護", False)]
    await _run_flow(_flow_stub, retries=3)
    assert _flow_stub["merge_calls"] == 1
    assert _flow_stub["update_calls"] == 0


# === 驗收標準 2：CI 失敗不嘗試合併、不誤判成功 ===============================


async def test_ci_failed_never_attempts_merge(_flow_stub):
    _flow_stub["ci"] = ("fail", "CI 失敗：unit tests")
    outcome, detail = await _run_flow(_flow_stub)
    assert outcome is MergeOutcome.CI_FAILED
    assert _flow_stub["merge_calls"] == 0


# === 驗收標準 4 + 防假修：behind 重試 → update-branch → 重抓新 sha 重等 CI ====


async def test_behind_retry_waits_on_new_sha_after_update_branch(_flow_stub):
    """behind/409 重試：每次失敗先 update-branch（改 head sha），下一輪須等「新 sha」的 CI。

    這是防『假修』的核心——若仍等舊 commit 的 CI，等於沒真正重試。
    """
    _flow_stub["pr"] = {"head": {"sha": "sha1"}, "mergeable": False, "mergeable_state": "behind"}
    _flow_stub["merge_seq"] = [
        (MergeOutcome.CONFLICT, "Base branch was modified（409）", True),
        (MergeOutcome.CONFLICT, "Base branch was modified（409）", True),
        (MergeOutcome.MERGED, "sha-final", False),
    ]
    outcome, _ = await _run_flow(_flow_stub, retries=3)
    assert outcome is MergeOutcome.MERGED
    assert _flow_stub["merge_calls"] == 3
    assert _flow_stub["update_calls"] == 2  # 前兩次失敗各修一次 stale
    # 三輪各等不同且遞進的 head sha（sha1 → sha2 → sha3），證明有重抓新 sha
    assert _flow_stub["waited_shas"] == ["sha1", "sha2", "sha3"]


async def test_retry_exhausted_then_give_up(_flow_stub):
    """可重試錯誤一直失敗 → 達上限放棄並回報，不無限重試。"""
    _flow_stub["pr"] = {"head": {"sha": "s"}, "mergeable": False, "mergeable_state": "behind"}
    _flow_stub["merge_seq"] = [(MergeOutcome.CONFLICT, "409", True)]
    outcome, detail = await _run_flow(_flow_stub, retries=2)
    assert outcome is MergeOutcome.CONFLICT
    assert "重試上限" in detail
    assert _flow_stub["merge_calls"] == 3  # retries=2 → 共 3 次


async def test_backoff_is_exponential_and_capped():
    """backoff 隨 attempt 指數成長且封頂 60s（驗收 4 的 backoff 行為）。"""
    assert publisher._backoff(0, 10) == 10
    assert publisher._backoff(1, 10) == 20
    assert publisher._backoff(2, 10) == 40
    assert publisher._backoff(5, 10) == 60.0  # 封頂


# === 驗收標準 3：逾時不無限阻塞，回明確逾時訊息 =============================


async def test_wait_for_ci_pending_then_timeout(monkeypatch):
    """CI 一直 pending → 在 timeout 內回 timeout，且回報含已等待秒數，不無限等。"""

    async def always_pending(sha):
        return [], {"state": "pending", "total_count": 1}

    slept = {"n": 0}

    async def fake_sleep(s):
        slept["n"] += 1

    monkeypatch.setattr(publisher, "_fetch_ci", lambda sha: always_pending(sha))
    state, detail = await publisher._wait_for_ci("sha", timeout=5, interval=1, sleep=fake_sleep)
    assert state == "timeout"
    assert "逾時" in detail
    assert slept["n"] <= 6  # 有界，不無限輪詢


# === 驗收標準 6：設定可調 ==================================================


async def test_settings_overridable_via_reload(monkeypatch):
    monkeypatch.setenv("TI_PUBLISH_CI_TIMEOUT", "111")
    monkeypatch.setenv("TI_PUBLISH_CI_INTERVAL", "3")
    monkeypatch.setenv("TI_PUBLISH_MERGE_RETRIES", "9")
    try:
        config.reload()
        assert config.PUBLISH_CI_TIMEOUT == 111
        assert config.PUBLISH_CI_INTERVAL == 3
        assert config.PUBLISH_MERGE_RETRIES == 9
    finally:
        for k in ("TI_PUBLISH_CI_TIMEOUT", "TI_PUBLISH_CI_INTERVAL", "TI_PUBLISH_MERGE_RETRIES"):
            monkeypatch.delenv(k, raising=False)
        config.reload()
