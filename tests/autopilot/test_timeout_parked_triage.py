"""Rule 2：歷史 timeout-parked 自動拆分分診（`autopilot._maybe_triage_timeout_parked`）。

背景：`_handle_task_timeout` 只在「新逾時當下」拆分；autosplit 關閉/當時拆空/操作者事後才調參的
歷史 parked（note 仍是 `task timeout after Ns`）從不被回頭觸及，長期躺成死水。本規則在主迴圈頂端每輪
挑至多 1 筆 Rule 1（`backlog.triage_failed` 確定性退回）退不了的 timeout-parked，重用 `_autosplit_task`
拆成更小子任務入列 pending，原任務維持 parked 並設 `split_done=True` 防下輪重揀。

純檔案 IO + monkeypatch（mock Expert.speak / _prepare_clone），不打 LLM/網路。
"""

from __future__ import annotations

import pytest

from studio import autopilot, backlog, config


class _Stop(BaseException):
    """跳出 `_main_loop` 無限迴圈用；繼承 BaseException 避免被 except Exception 吃掉。"""


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_STATE_DIR", tmp_path / "ap")
    monkeypatch.setattr(config, "AUTOPILOT_DRYRUN", False)
    monkeypatch.setattr(config, "AUTOPILOT_TIMEOUT_AUTOSPLIT", True)
    monkeypatch.setattr(config, "AUTOPILOT_SPLIT_MAX_DEPTH", 2)
    monkeypatch.setattr(config, "AUTOPILOT_SPLIT_MAX_SUBTASKS", 4)
    monkeypatch.setattr(config, "AUTOPILOT_TASK_TIMEOUT", 7200)
    monkeypatch.setattr(autopilot, "_prepare_clone", _fake_clone)
    return tmp_path


async def _fake_clone(*_a, **_k):
    return "/tmp/does-not-matter"


def _patch_expert(monkeypatch, reply: str):
    import studio.experts as experts_mod

    class _FakeExpert:
        calls = 0

        def __init__(self, *a, **k):
            pass

        async def speak(self, prompt, on_event):
            type(self).calls += 1
            type(self).last_prompt = prompt
            return reply

        async def stop(self):
            return None

    monkeypatch.setattr(experts_mod, "Expert", _FakeExpert)
    return _FakeExpert


def _parked_timeout_task(*, note_secs: int = 7200, depth: int = 0, **fields):
    """建立一筆 timeout-parked 任務：note＝`task timeout after {note_secs}s`。"""
    t = backlog.add("範圍過大的任務")
    note = f"{autopilot._TIMEOUT_NOTE_PREFIX} {note_secs}s — 需拆分或縮小範圍"
    return backlog.set_status(t["id"], "parked", note=note, split_depth=depth, **fields)


def _reload(tid: int):
    return next(t for t in backlog.list_tasks() if t["id"] == tid)


# --- 揀選條件（_timeout_parked_candidates）黑白樣本 ---------------------------


def test_candidate_picks_retried_task(state):
    """白樣本：已被 Rule 1 退回重試過（timeout_retried=True）者由 Rule 2 接手。"""
    t = _parked_timeout_task(timeout_retried=True)
    picked = autopilot._timeout_parked_candidates()
    assert [c["id"] for c in picked] == [t["id"]]


def test_candidate_picks_n_ge_current(state):
    """白樣本：note 秒數 N ≥ 現行上限（調高上限也白搭）→ Rule 1 不適用，Rule 2 接手。"""
    t = _parked_timeout_task(note_secs=7200)  # == 現行 7200
    picked = autopilot._timeout_parked_candidates()
    assert [c["id"] for c in picked] == [t["id"]]


def test_candidate_skips_rule1_applicable(state):
    """黑樣本：未重試過且 N < 現行上限 → Rule 1 適用（值得原樣重試），Rule 2 不搶。"""
    _parked_timeout_task(note_secs=3600)  # < 現行 7200，且無 timeout_retried
    assert autopilot._timeout_parked_candidates() == []


def test_candidate_skips_split_done(state):
    """黑樣本：已被 Rule 2 處理過（split_done=True）不再重揀。"""
    _parked_timeout_task(note_secs=7200, split_done=True)
    assert autopilot._timeout_parked_candidates() == []


def test_candidate_skips_depth_cap_note(state):
    """黑樣本：深度上限變體 note 不含 `task timeout after` 前綴，天然不入選（交人工）。"""
    t = backlog.add("達深度上限的任務")
    backlog.set_status(
        t["id"], "parked", note="逾時且已達自動拆分深度上限（2）——需人工拆分或縮小範圍"
    )
    assert autopilot._timeout_parked_candidates() == []


def test_candidate_skips_non_parked(state):
    """黑樣本：非 parked（failed/pending）即使 note 相符也不入選。"""
    t = backlog.add("失敗任務")
    backlog.set_status(t["id"], "failed", note=f"{autopilot._TIMEOUT_NOTE_PREFIX} 9000s — x")
    assert autopilot._timeout_parked_candidates() == []


# --- _maybe_triage_timeout_parked 行為 --------------------------------------


@pytest.mark.asyncio
async def test_splits_and_marks_original(state, monkeypatch):
    """白樣本：拆出子任務入列 pending（split_depth=父+1），原任務 parked + split_done=True + note 更新。"""
    _patch_expert(monkeypatch, "任務: 實作 A 並補測\n任務: 修復 B 並補測")
    t = _parked_timeout_task(timeout_retried=True)

    await autopilot._maybe_triage_timeout_parked()

    orig = _reload(t["id"])
    assert orig["status"] == "parked"
    assert orig["split_done"] is True
    assert "已自動拆為" in orig["note"]
    children = [c for c in backlog.list_tasks() if c.get("source") == "split"]
    assert {c["title"] for c in children} == {"實作 A 並補測", "修復 B 並補測"}
    assert all(c["status"] == "pending" and c.get("split_depth") == 1 for c in children)


@pytest.mark.asyncio
async def test_split_done_prevents_repick_next_round(state, monkeypatch):
    """拆分後原任務下輪不再被揀走（split_done 收斂，防無限循環）。"""
    fake = _patch_expert(monkeypatch, "任務: 實作 A 並補測\n任務: 修復 B 並補測")
    _parked_timeout_task(timeout_retried=True)

    await autopilot._maybe_triage_timeout_parked()
    assert fake.calls == 1
    assert autopilot._timeout_parked_candidates() == [], "已標 split_done，下輪不再入選"

    await autopilot._maybe_triage_timeout_parked()  # 第二輪
    assert fake.calls == 1, "下輪不得再叫專家"


@pytest.mark.asyncio
async def test_only_one_per_round(state, monkeypatch):
    """每輪最多處理 1 筆（_TIMEOUT_SPLIT_PER_ROUND）。"""
    fake = _patch_expert(monkeypatch, "任務: 實作 A 並補測")
    _parked_timeout_task(timeout_retried=True)
    _parked_timeout_task(timeout_retried=True)
    _parked_timeout_task(timeout_retried=True)

    await autopilot._maybe_triage_timeout_parked()

    assert fake.calls == 1, "每輪僅拆 1 筆"
    done = [c for c in backlog.list_tasks() if c.get("split_done")]
    assert len(done) == 1
    # 尚有 2 筆未處理，留待後續輪次
    assert len(autopilot._timeout_parked_candidates()) == 2


@pytest.mark.asyncio
async def test_depth_cap_no_split(state, monkeypatch):
    """黑樣本：深度達上限者不拆——不叫專家，標 split_done + 深度上限 note 導向人工。"""
    fake = _patch_expert(monkeypatch, "任務: 不該被產生的子任務")
    t = _parked_timeout_task(note_secs=7200, depth=2)  # == MAX_DEPTH

    await autopilot._maybe_triage_timeout_parked()

    orig = _reload(t["id"])
    assert orig["status"] == "parked" and orig["split_done"] is True
    assert "深度上限" in orig["note"]
    assert not [c for c in backlog.list_tasks() if c.get("source") == "split"]
    assert fake.calls == 0, "達上限根本不該叫專家"


@pytest.mark.asyncio
async def test_empty_split_marks_split_done(state, monkeypatch):
    """拆不出有效子任務（全雜訊/busywork）→ 標 split_done 防重複打 LLM，導向人工。"""
    _patch_expert(monkeypatch, "任務: 實作需求\n任務: 收尾驗收 QA pass 落檔 sha256")
    t = _parked_timeout_task(timeout_retried=True)

    await autopilot._maybe_triage_timeout_parked()

    orig = _reload(t["id"])
    assert orig["status"] == "parked" and orig["split_done"] is True
    assert "拆不出" in orig["note"]
    assert not [c for c in backlog.list_tasks() if c.get("source") == "split"]


@pytest.mark.asyncio
async def test_split_exception_marks_split_done(state, monkeypatch):
    """拆分過程拋例外也不得中斷主迴圈：吞掉、標 split_done 待人工。"""

    async def _boom(*_a, **_k):
        raise RuntimeError("clone 掛了")

    monkeypatch.setattr(autopilot, "_prepare_clone", _boom)
    t = _parked_timeout_task(timeout_retried=True)

    await autopilot._maybe_triage_timeout_parked()  # 不得拋出

    orig = _reload(t["id"])
    assert orig["status"] == "parked" and orig["split_done"] is True
    assert "自動拆分失敗" in orig["note"]


@pytest.mark.asyncio
async def test_disabled_or_dryrun_noop(state, monkeypatch):
    """autosplit 關閉或 dryrun → 完全不動 backlog。"""
    fake = _patch_expert(monkeypatch, "任務: 實作 A 並補測")
    t = _parked_timeout_task(timeout_retried=True)

    monkeypatch.setattr(config, "AUTOPILOT_TIMEOUT_AUTOSPLIT", False)
    await autopilot._maybe_triage_timeout_parked()
    monkeypatch.setattr(config, "AUTOPILOT_TIMEOUT_AUTOSPLIT", True)
    monkeypatch.setattr(config, "AUTOPILOT_DRYRUN", True)
    await autopilot._maybe_triage_timeout_parked()

    orig = _reload(t["id"])
    assert orig.get("split_done") is not True
    assert fake.calls == 0


@pytest.mark.asyncio
async def test_rule1_and_rule2_share_backlog_without_double_processing(state, monkeypatch):
    """跨規則整合：Rule 1 退回與 Rule 2 拆分同場執行，標記互斥且不重複處理同一筆。"""
    fake = _patch_expert(monkeypatch, "任務: 實作 A 並補測\n任務: 修復 B 並補測")
    retry = _parked_timeout_task(note_secs=3600, attempts=3)
    split = _parked_timeout_task(note_secs=7200, attempts=3)
    already_split = _parked_timeout_task(note_secs=3600, split_done=True)
    manual = backlog.add("人工歸檔任務")
    backlog.set_status(manual["id"], "parked", note="人工歸檔，等需求確認")

    stats = backlog.triage_failed()

    assert stats == {"retried": 0, "parked": 0, "revived": 0, "unparked": 1}
    assert [c["id"] for c in autopilot._timeout_parked_candidates()] == [split["id"]]

    await autopilot._maybe_triage_timeout_parked()

    retry_cur = _reload(retry["id"])
    split_cur = _reload(split["id"])
    already_split_cur = _reload(already_split["id"])
    manual_cur = _reload(manual["id"])
    children = [c for c in backlog.list_tasks() if c.get("source") == "split"]

    assert retry_cur["status"] == "pending"
    assert retry_cur["attempts"] == 0
    assert retry_cur["timeout_retried"] is True
    assert retry_cur.get("split_done") is not True

    assert split_cur["status"] == "parked"
    assert split_cur["split_done"] is True
    assert split_cur.get("timeout_retried") is not True
    assert "已自動拆為" in split_cur["note"]

    assert already_split_cur["status"] == "parked"
    assert already_split_cur["split_done"] is True
    assert already_split_cur.get("timeout_retried") is not True
    assert manual_cur["status"] == "parked"
    assert manual_cur.get("timeout_retried") is not True
    assert manual_cur.get("split_done") is not True

    assert fake.calls == 1
    assert {c["title"] for c in children} == {"實作 A 並補測", "修復 B 並補測"}
    assert all(c["status"] == "pending" and c.get("split_depth") == 1 for c in children)
    assert all(f"#{split['id']}" in c["detail"] for c in children)
    assert autopilot._timeout_parked_candidates() == []


@pytest.mark.asyncio
async def test_main_loop_runs_rule2_before_fetching_pending(state, monkeypatch):
    """主迴圈合約：Rule 2 與 failed triage 同在取 pending 前觸發，且順序穩定。"""
    calls: list[str] = []

    monkeypatch.setattr(config, "AUTOPILOT_QUOTA_GATE", False)
    monkeypatch.setattr(config, "autopilot_paused", lambda: False)
    monkeypatch.setattr(autopilot, "_shutdown_requested", False)
    monkeypatch.setattr(autopilot, "_daily_pr_budget_exceeded", lambda: False)
    monkeypatch.setattr(autopilot, "_maybe_triage_failed", lambda: calls.append("failed"))

    async def fake_timeout_triage():
        calls.append("timeout_parked")

    async def fake_boundary_redeploy():
        calls.append("boundary")

    async def fake_reconcile():
        calls.append("reconcile")

    def fake_next_pending():
        calls.append("next_pending")
        raise _Stop

    monkeypatch.setattr(autopilot, "_maybe_triage_timeout_parked", fake_timeout_triage)
    monkeypatch.setattr(autopilot, "_recover_stale_in_progress", lambda: calls.append("recover"))
    monkeypatch.setattr(autopilot, "_maybe_boundary_redeploy", fake_boundary_redeploy)
    monkeypatch.setattr(autopilot, "_maybe_reconcile_open_prs", fake_reconcile)
    monkeypatch.setattr(autopilot.backlog, "next_pending", fake_next_pending)

    with pytest.raises(_Stop):
        await autopilot._main_loop(startup_sig=0.0)

    assert calls == ["failed", "timeout_parked", "recover", "boundary", "reconcile", "next_pending"]
