"""run_one_task 成功路徑的 PR/部署追溯落檔：set_status done 帶 pr/merged_branch/deploy_msg。

MergeResult 為 (ok, msg) 的向後相容擴充——既有 `ok, msg = ...` 解包不變，
成功時額外攜帶 pr_number/branch；失敗/dryrun 純 tuple 時以 getattr 容錯取 None/""。
"""

from __future__ import annotations

import pytest

from studio import autonomy, autopilot, config

_RESULT_OK = {
    "completed": True,
    "followups": [],
    "followup_items": [],
    "core_changes": [],
}


def _common_mocks(monkeypatch, tmp_path, statuses, *, merge_result, idle=True, deploy=None):
    clone = tmp_path / "clone"
    clone.mkdir()

    async def fake_prepare_clone():
        return clone

    class FakeSession:
        def __init__(self, *_args, **_kwargs):
            pass

        async def run(self, _requirement):
            return _RESULT_OK

    async def fake_gate(*_args, **_kwargs):
        return (True, "")

    async def fake_merge(*_args, **_kwargs):
        return merge_result

    async def fake_idle():
        return idle

    async def fake_redeploy():
        return deploy if deploy is not None else (True, "重佈成功：abc12345 → def67890")

    monkeypatch.setattr(autopilot, "_prepare_clone", fake_prepare_clone)
    monkeypatch.setattr(autopilot, "StudioSession", FakeSession)
    monkeypatch.setattr(autopilot.history, "start_session", lambda *a, **k: None)
    monkeypatch.setattr(autopilot.history, "record_event", lambda *a, **k: None)
    monkeypatch.setattr(autopilot.history, "finish_session", lambda *a, **k: None)
    monkeypatch.setattr(
        autopilot.backlog,
        "set_status",
        lambda task_id, status, **kw: statuses.append((task_id, status, kw)),
    )
    monkeypatch.setattr(autopilot.backlog, "add_items", lambda *a, **k: 0)
    monkeypatch.setattr(autopilot.backlog, "add_many", lambda *a, **k: 0)
    monkeypatch.setattr(autopilot.backlog, "add", lambda *a, **k: None)
    monkeypatch.setattr(autopilot.backlog, "route_core_changes", lambda *a, **k: 0)
    monkeypatch.setattr(autopilot, "_gate_lint", fake_gate)
    monkeypatch.setattr(autopilot, "_gate_collect_without_sdk", fake_gate)
    monkeypatch.setattr(autopilot, "_gate_tests", fake_gate)
    monkeypatch.setattr(autopilot, "_commit_push_merge", fake_merge)
    monkeypatch.setattr(autopilot, "_wait_until_idle", fake_idle)
    monkeypatch.setattr(autopilot.deploy, "redeploy", fake_redeploy)


def test_merge_result_unpacks_as_two_tuple():
    """既有呼叫端契約：MergeResult 解包/相等比較仍是 (ok, msg) 二元組。"""
    res = autopilot.MergeResult(True, "已合併", pr_number=42, branch="autopilot/task-7")
    ok, msg = res
    assert (ok, msg) == (True, "已合併")
    assert res == (True, "已合併")
    assert res.pr_number == 42 and res.branch == "autopilot/task-7"


@pytest.mark.asyncio
async def test_done_records_pr_branch_and_deploy_msg(monkeypatch, tmp_path):
    statuses: list = []
    _common_mocks(
        monkeypatch,
        tmp_path,
        statuses,
        merge_result=autopilot.MergeResult(
            True, "已 squash-merge", pr_number=123, branch="autopilot/task-9"
        ),
    )
    await autopilot.run_one_task({"id": 9, "title": "落檔測試"})
    tid, status, kw = statuses[-1]
    assert (tid, status) == (9, "done")
    assert kw["pr"] == 123
    assert kw["merged_branch"] == "autopilot/task-9"
    assert "abc12345 → def67890" in kw["deploy_msg"]


@pytest.mark.asyncio
async def test_done_tolerates_plain_tuple_merge_result(monkeypatch, tmp_path):
    """dryrun 等純 tuple 回傳：getattr 容錯取 None/""，不炸也不虛構 PR 編號。"""
    statuses: list = []
    _common_mocks(
        monkeypatch,
        tmp_path,
        statuses,
        merge_result=(True, "[dryrun] 會 push 並 squash-merge"),
        idle=False,  # 略過重佈 → 無 deploy_msg
    )
    await autopilot.run_one_task({"id": 5, "title": "dryrun"})
    tid, status, kw = statuses[-1]
    assert (tid, status) == (5, "done")
    assert kw["pr"] is None and kw["merged_branch"] == ""
    assert "deploy_msg" not in kw


@pytest.mark.asyncio
async def test_redeploy_failure_still_records_traceability(monkeypatch, tmp_path):
    """重佈失敗：failed 也帶 pr/merged_branch/deploy_msg，事後可追是哪個 PR 弄壞部署。"""
    statuses: list = []
    _common_mocks(
        monkeypatch,
        tmp_path,
        statuses,
        merge_result=autopilot.MergeResult(True, "已合併", pr_number=77, branch="autopilot/task-3"),
        deploy=(False, "重佈失敗（health check 未過）→ 回滾成功"),
    )
    monkeypatch.setattr(autopilot, "_pause", lambda *_a, **_k: None)
    await autopilot.run_one_task({"id": 3, "title": "壞部署"})
    tid, status, kw = statuses[-1]
    assert (tid, status) == (3, "failed")
    assert kw["pr"] == 77 and "重佈失敗" in kw["deploy_msg"]


@pytest.mark.asyncio
async def test_managed_shadow_generates_local_evidence_without_merge_or_deploy(
    monkeypatch, tmp_path
):
    """完整 core 路徑到客觀閘門後必在 merge 前停下，不只單測 policy evaluator。"""
    statuses: list = []
    _common_mocks(
        monkeypatch,
        tmp_path,
        statuses,
        merge_result=autopilot.MergeResult(True, "不應被呼叫"),
    )
    monkeypatch.setattr(config, "AUTOPILOT_STATE_DIR", tmp_path / "state")
    deploy_dir = tmp_path / "deploy"
    deploy_dir.mkdir()
    monkeypatch.setattr(config, "AUTOPILOT_DEPLOY_DIR", deploy_dir)
    monkeypatch.setattr(config, "FAST_LANE", False)
    autonomy.ensure_policy(autonomy.CORE_PROJECT_ID)

    async def forbidden_merge(*args, **kwargs):
        raise AssertionError("shadow 不得進入 commit/push/merge")

    async def forbidden_deploy(*args, **kwargs):
        raise AssertionError("shadow 不得部署")

    monkeypatch.setattr(autopilot, "_commit_push_merge", forbidden_merge)
    monkeypatch.setattr(autopilot.deploy, "redeploy", forbidden_deploy)
    await autopilot.run_one_task(
        {"id": 31, "title": "shadow 證據任務", "risk": "medium", "eligible": True}
    )

    parked = [row for row in statuses if row[1] == "parked"]
    assert parked and "shadow 模式" in parked[-1][2]["note"]
    phases = {
        (event.get("payload") or {}).get("phase")
        for event in autonomy.read_events(1)
        if event.get("outcome") == "shadow_only"
    }
    assert {"planning", "change", "merge"} <= phases
