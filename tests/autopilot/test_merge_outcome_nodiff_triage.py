"""Merge 階段結局改善（完成率修法 C + D）。

背景（完成率診斷）：
- C「零 diff no-op」：audit 22 筆 merge_failed 中 16 筆是「沒有產生任何變更」——多為收尾驗收/
  QA 類元任務，本就無事可做，卻被當合併失敗白燒 3 次 session 才 failed。改為以 no_changes
  旗標收斂成 parked no-op：不燒重試、不落失敗桶、audit 走獨立 outcome。
- D「暫時性 merge 失敗歸 infra」：ls-remote/push/開 PR 的網路層暫時性失敗（DNS/連線/5xx/逾時）
  附「unreachable」標記 → backlog.INFRA_FAILURE_RE 命中 → triage 達上限後自動重排；認證/權限
  等實質失敗不附（附了會讓 triage 無限重排），達上限即永久 failed。
"""

from __future__ import annotations

import pytest

from studio import autopilot, backlog
from studio.autopilot import MergeResult

# ---------------------------------------------------------------------------
# C）零 diff → parked no-op（不重試、不 failed、audit outcome=no_changes）
# ---------------------------------------------------------------------------


def _mocks(monkeypatch, tmp_path, *, merge_result, statuses, audits, result=None, gate_calls=None):
    clone = tmp_path / "clone"
    clone.mkdir()
    result = result or {
        "completed": True,
        "shippable": True,
        "followups": [],
        "followup_items": [],
        "core_changes": [],
    }

    async def fake_prepare_clone():
        return clone

    class FakeSession:
        def __init__(self, *_a, **_k):
            pass

        async def run(self, _req):
            return result

    async def fake_gate(*_a, **_k):
        if gate_calls is not None:
            gate_calls.append(True)
        return (True, "")

    async def fake_merge(*_a, **_k):
        return merge_result

    async def fake_idle():
        return False  # 略過重佈，聚焦狀態判定

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
    monkeypatch.setattr(autopilot, "_append_audit", lambda rec: audits.append(rec))
    monkeypatch.setattr(autopilot.config, "AUTOPILOT_DRYRUN", False)


@pytest.mark.asyncio
async def test_no_changes_parks_noop_not_failed(monkeypatch, tmp_path):
    statuses: list = []
    audits: list = []
    merge_res = MergeResult(False, "沒有產生任何變更（無 commit 可合併）", no_changes=True)
    _mocks(monkeypatch, tmp_path, merge_result=merge_res, statuses=statuses, audits=audits)

    await autopilot.run_one_task({"id": 21, "title": "收尾驗收：確認守門到位"})

    tid, status, kw = statuses[-1]
    assert (tid, status) == (21, "parked"), f"零 diff 應收斂為 parked no-op：{statuses[-1]!r}"
    assert "無變更" in kw.get("note", ""), "note 須標明 no-op（非失敗）"
    assert not any(s[1] == "failed" for s in statuses), "no-op 不得落入失敗桶"
    # attempts 未被 _handle_gate_failure 遞增（沒燒重試）
    assert all("attempts" not in s[2] or s[2].get("attempts") is None for s in statuses[1:])
    # audit 走獨立 outcome，不污染 merge_failed 桶
    assert (
        audits and audits[-1]["outcome"] == "no_changes"
    ), f"audit outcome 應為 no_changes：{audits[-1]!r}"


@pytest.mark.asyncio
async def test_real_merge_failure_still_gate_fails(monkeypatch, tmp_path):
    """黑樣本：真的合併失敗（非 no_changes）仍走 _handle_gate_failure（退回重試/最終 failed），
    audit 仍記 merge_failed——證明 no-op 分流沒有誤放行真失敗。"""
    statuses: list = []
    audits: list = []
    merge_res = MergeResult(
        False, "CI 未過或合併失敗（ci_failed）：test (3.11)", pr_number=7, branch="b"
    )
    _mocks(monkeypatch, tmp_path, merge_result=merge_res, statuses=statuses, audits=audits)

    await autopilot.run_one_task({"id": 22, "title": "真的改壞了", "attempts": 0})

    tid, status, kw = statuses[-1]
    # 首次未過（attempts 0，上限預設 3）→ 退回 pending 重試，且帶 [merge] 標籤
    assert (tid, status) == (22, "pending"), f"真失敗應走 gate 重試：{statuses[-1]!r}"
    assert "[merge]" in kw.get("note", "")
    assert audits[-1]["outcome"] == "merge_failed", "真失敗 audit 仍為 merge_failed"


# ---------------------------------------------------------------------------
# D）暫時性 merge 失敗 → 附 unreachable 標記（triage 可重排）；認證失敗不附
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "out",
    [
        "fatal: unable to access 'https://github.com/...': Could not resolve host: github.com",
        "ssh: connect to host github.com port 22: Connection timed out",
        "error: RPC failed; HTTP 503 curl 22",
        "ls-remote 逾時 60s",
    ],
)
def test_network_transient_tagged_infra(out):
    note = autopilot._merge_fail_note("push 失敗", out)
    assert "unreachable" in note, f"網路暫時性應附 infra 標記：{note!r}"
    assert backlog.INFRA_FAILURE_RE.search(
        note
    ), "標記後須被 triage 的 INFRA_FAILURE_RE 命中（會自動重排）"


@pytest.mark.parametrize(
    "out",
    [
        "remote: Permission to x/Ti.git denied to bot.\nfatal: unable to read from remote",
        "fatal: Authentication failed for 'https://github.com/x/Ti.git/'",
        "remote: 403 Forbidden",
        "fatal: could not read Username for 'https://github.com': terminal prompts disabled",
    ],
)
def test_auth_failure_not_tagged_infra(out):
    note = autopilot._merge_fail_note("push 失敗", out)
    assert (
        "unreachable" not in note
    ), f"認證/權限實質失敗不得附 infra 標記（否則 triage 無限重排）：{note!r}"
    assert not backlog.INFRA_FAILURE_RE.search(note), "認證失敗不得被 triage 當可重試"


def test_merge_result_no_changes_flag_defaults_false():
    plain = MergeResult(True, "ok")
    assert plain.no_changes is False, "預設 no_changes 必須為 False（不影響既有路徑）"
    assert (plain[0], plain[1]) == (True, "ok"), "MergeResult 仍以 (ok, msg) 解包"
    noop = MergeResult(False, "x", no_changes=True)
    assert noop.no_changes is True and noop == (False, "x")
