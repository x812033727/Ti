"""合併遇分支落後（BEHIND）自動更新分支重試——機械性 BEHIND 不再退回整場任務。

生產案例（2026-07-04 12:44）：任務 #250 兩小時討論產出 PR #283，合併時
mergeable_state=behind（分支保護要求與 base 同步；討論期間 main 前進）→ PUT merge 405
（不可重試）→ 舊 `_merge_flow` 在終局路徑直接退回 → autopilot 關 PR、刪分支、整場任務
重跑，兩小時成果全丟。修正後：405 終局前重查即時狀態，確為 behind 且還有額度即
update-branch → 重等新 head 的 CI → 重試合併（額度 `TI_MERGE_BEHIND_RETRIES`，預設 2）。

涵蓋：
- behind → update-branch → CI 綠 → 合併成功（且重等「新」head sha 的 CI，非假修）。
- behind → update-branch → CI 紅 → 維持原退回（CI_FAILED，不再嘗試合併）。
- behind 自動更新額度用盡 → 退回（CONFLICT），不無限追趕，detail 註明已追趕輪數。
- dirty（真衝突）不觸發 update-branch，直接退回。
- 非 behind 的失敗路徑（blocked 405／409 可重試上限）行為不變。
- TI_MERGE_BEHIND_RETRIES=0 恢復舊行為（behind 直接退回，零 update／零退避）。
- 繁中 log 訊息與 config reload 連動。
"""

from __future__ import annotations

import logging

import pytest

from studio import config, publisher
from studio.publisher import MergeOutcome

# 405（不可重試）——分支保護要求與 base 同步時，落後 PR 直接 merge 的典型回應。
_BLOCKED_405 = (MergeOutcome.BLOCKED, "不可合併／受保護（405）：not up to date", False)


@pytest.fixture
def _flow(monkeypatch):
    """鏡射 test_publisher_ci_merge 的 stub 慣例；update-branch 會改 head sha 模擬新 commit。

    - `pr`：目前 PR 狀態（_get_pr_status 回傳值；terminal 重查也讀同一份）。
    - `merge_seq`：依序回放的 _merge_pr 結果（超出以最後一筆重複）。
    - `after_update`：可選 callable，模擬 update-branch 後的世界變化（如 behind→clean）。
    """
    st = {
        "pr": {"head": {"sha": "sha1"}, "mergeable": False, "mergeable_state": "behind"},
        "ci": ("pass", "ok"),
        "merge_seq": [_BLOCKED_405],
        "merge_calls": 0,
        "updates": 0,
        "waited_shas": [],
        "sleeps": [],
        "after_update": None,
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
        st["updates"] += 1
        # 模擬 update-branch 產生新 head commit（sha1 → sha2 → …）
        st["pr"] = {**st["pr"], "head": {"sha": f"sha{st['updates'] + 1}"}}
        if st["after_update"]:
            st["after_update"](st)
        return True

    async def fake_sleep(s):
        st["sleeps"].append(s)

    monkeypatch.setattr(publisher, "_get_pr_status", fake_status)
    monkeypatch.setattr(publisher, "_wait_for_ci", fake_wait)
    monkeypatch.setattr(publisher, "_merge_pr", fake_merge)
    monkeypatch.setattr(publisher, "_update_branch", fake_update)
    # 測試不依賴環境變數：顯式固定 behind 額度為預設 2
    monkeypatch.setattr(config, "MERGE_BEHIND_RETRIES", 2)
    st["sleep"] = fake_sleep
    return st


async def _run(st, retries=3, ci_interval=5):
    return await publisher._merge_flow(
        7, {}, ci_timeout=60, ci_interval=ci_interval, retries=retries, sleep=st["sleep"]
    )


# === behind → update-branch → CI 綠 → 合併成功 ==============================


@pytest.mark.asyncio
async def test_behind_405_updates_branch_then_merges(_flow):
    """生產案例主修：behind + merge 405 → update-branch → CI 綠 → 重試合併成功。"""

    def clean_after_update(st):
        st["pr"] = {**st["pr"], "mergeable": True, "mergeable_state": "clean"}

    _flow["after_update"] = clean_after_update
    _flow["merge_seq"] = [_BLOCKED_405, (MergeOutcome.MERGED, "sha-final", False)]
    outcome, detail = await _run(_flow)
    assert outcome is MergeOutcome.MERGED
    assert _flow["updates"] == 1
    assert _flow["merge_calls"] == 2
    # 防假修：update 後第二輪必須重等「新」head sha 的 CI，而非舊 commit
    assert _flow["waited_shas"] == ["sha1", "sha2"]


@pytest.mark.asyncio
async def test_behind_recovery_logs_zh_message(_flow, caplog):
    """繁中 log：「PR #N 落後 base，自動更新分支後重試合併（第 k/2 輪）」。"""

    def clean_after_update(st):
        st["pr"] = {**st["pr"], "mergeable": True, "mergeable_state": "clean"}

    _flow["after_update"] = clean_after_update
    _flow["merge_seq"] = [_BLOCKED_405, (MergeOutcome.MERGED, "sha-final", False)]
    with caplog.at_level(logging.INFO, logger="ti.publisher"):
        await _run(_flow)
    assert any(
        "PR #7 落後 base，自動更新分支後重試合併（第 1/2 輪）" in r.getMessage()
        for r in caplog.records
    )


# === behind → update-branch → CI 紅 → 維持原退回 ============================


@pytest.mark.asyncio
async def test_behind_update_then_ci_red_falls_back(_flow):
    """update 後新 head 的 CI 紅 → 維持原退回行為（CI_FAILED），不再嘗試合併。"""

    def ci_red_after_update(st):
        st["ci"] = ("fail", "CI 失敗：unit tests")

    _flow["after_update"] = ci_red_after_update
    outcome, detail = await _run(_flow)
    assert outcome is MergeOutcome.CI_FAILED
    assert _flow["updates"] == 1
    assert _flow["merge_calls"] == 1  # CI 紅後絕不再嘗試合併


# === behind 額度用盡 → 退回，不無限追趕 =====================================


@pytest.mark.asyncio
async def test_behind_retries_exhausted_gives_up(_flow):
    """base 高頻前進（update 後仍 behind）→ 額度（2）用盡即退回 CONFLICT，有界。"""
    outcome, detail = await _run(_flow)  # merge 永遠 405、狀態永遠 behind
    assert outcome is MergeOutcome.CONFLICT
    assert "落後" in detail
    assert "已自動更新分支 2 輪" in detail
    assert _flow["updates"] == 2  # 額度 2 → 恰好兩次 update
    assert _flow["merge_calls"] == 3  # 首次 + 兩輪追趕，之後放棄


# === dirty（真衝突）不觸發 update，直接退回 =================================


@pytest.mark.asyncio
async def test_dirty_does_not_trigger_update_branch(_flow):
    """dirty（真衝突）→ 不 update-branch、直接退回 CONFLICT（維持原行為）。"""
    _flow["pr"] = {"head": {"sha": "s"}, "mergeable": False, "mergeable_state": "dirty"}
    outcome, detail = await _run(_flow)
    assert outcome is MergeOutcome.CONFLICT
    assert "衝突" in detail
    assert _flow["updates"] == 0
    assert _flow["merge_calls"] == 1


# === 非 behind 失敗路徑不變 =================================================


@pytest.mark.asyncio
async def test_blocked_non_behind_405_unchanged(_flow):
    """blocked（缺審核／保護規則）405 → 行為不變：不 update、不退避、一次即退回。"""
    _flow["pr"] = {"head": {"sha": "s"}, "mergeable": True, "mergeable_state": "blocked"}
    outcome, detail = await _run(_flow)
    assert outcome is MergeOutcome.BLOCKED
    assert "審核" in detail
    assert _flow["updates"] == 0
    assert _flow["merge_calls"] == 1
    assert _flow["sleeps"] == []


@pytest.mark.asyncio
async def test_retryable_409_path_unchanged(_flow):
    """可重試 409 路徑不變：仍走既有 backoff + update-branch，上限後回「重試上限」。"""
    _flow["merge_seq"] = [(MergeOutcome.CONFLICT, "Base branch was modified（409）", True)]
    outcome, detail = await _run(_flow, retries=1, ci_interval=5)
    assert outcome is MergeOutcome.CONFLICT
    assert "重試上限" in detail
    assert _flow["merge_calls"] == 2  # retries=1 → 1 + 1
    assert _flow["updates"] == 1  # 既有 reactive 路徑：每次可重試失敗前 update 一次
    assert _flow["sleeps"] == [5]  # 既有指數 backoff（attempt 0）


# === TI_MERGE_BEHIND_RETRIES=0 恢復舊行為 ===================================


@pytest.mark.asyncio
async def test_behind_retries_zero_restores_old_behavior(_flow, monkeypatch):
    """額度 0＝停用：behind + 405 直接退回 CONFLICT，零 update、零退避（＝修正前行為）。"""
    monkeypatch.setattr(config, "MERGE_BEHIND_RETRIES", 0)
    outcome, detail = await _run(_flow)
    assert outcome is MergeOutcome.CONFLICT
    assert "落後" in detail
    assert _flow["updates"] == 0
    assert _flow["merge_calls"] == 1
    assert _flow["sleeps"] == []


# === config 連動 =============================================================


def test_config_has_merge_behind_retries():
    assert isinstance(config.MERGE_BEHIND_RETRIES, int)


def test_config_reload_picks_up_merge_behind_retries(monkeypatch):
    monkeypatch.setenv("TI_MERGE_BEHIND_RETRIES", "5")
    try:
        config.reload()
        assert config.MERGE_BEHIND_RETRIES == 5
    finally:
        monkeypatch.delenv("TI_MERGE_BEHIND_RETRIES", raising=False)
        config.reload()
