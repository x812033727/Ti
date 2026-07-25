"""Admission 持久化與認領的原子儲存契約（無模型、無網路）。"""

from __future__ import annotations

import asyncio

import pytest

from studio import backlog, config


@pytest.fixture(autouse=True)
def _state(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_STATE_DIR", tmp_path / "ap")
    monkeypatch.setattr(backlog, "_read_cache", {}, raising=False)


def _payload(outcome: str = "ready") -> tuple[dict, dict]:
    contract = {
        "version": 1,
        "outcome": "變更可被客觀驗收",
        "kind": "implementation",
        "targets": ["studio/backlog.py"],
        "acceptance": ["pytest", "reviewable diff"],
        "constraints": [],
        "external_writes": [],
    }
    admission = {
        "outcome": outcome,
        "reasons": [],
        "missing_fields": [],
        "audit": {"contract_hash": "a" * 64, "repo_sha": "b" * 40},
    }
    return contract, admission


def test_pending_snapshots_are_sorted_copies(monkeypatch):
    monkeypatch.setattr(config, "TASK_ADMISSION_MODE", "enforce")
    backlog.add("自動", source="discovered")
    human = backlog.add("人工", source="manual")

    rows = backlog.pending_snapshots()
    rows[0]["title"] = "呼叫端誤改"

    assert rows[0]["id"] == human["id"]
    assert backlog.get(human["id"])["title"] == "人工"


@pytest.mark.parametrize("mode", ["off", "shadow"])
def test_non_enforcing_modes_keep_legacy_priority_then_fifo_order(monkeypatch, mode):
    monkeypatch.setattr(config, "TASK_ADMISSION_MODE", mode)
    automated = backlog.add("先入列的自動任務", source="discovered")
    backlog.add("後入列的人工任務", source="manual")

    assert backlog.next_pending()["id"] == automated["id"]
    assert backlog.claim_next(lambda _task: True)["id"] == automated["id"]


def test_commit_admission_records_without_consuming_attempt():
    task = backlog.add("補 admission")
    fingerprint = backlog.task_fingerprint(task)
    contract, admission = _payload()

    result, error = backlog.commit_admission(
        task["id"],
        fingerprint,
        contract=contract,
        admission=admission,
        transition="record",
    )
    contract["outcome"] = "呼叫端事後誤改"

    assert error == ""
    assert result.attempts_before == 0
    assert result.task["status"] == "pending"
    assert result.task["attempts"] == 0
    assert backlog.get(task["id"])["contract"]["outcome"] == "變更可被客觀驗收"


def test_commit_admission_claim_is_atomic_and_returns_preclaim_attempts():
    task = backlog.add("原子認領")
    contract, admission = _payload()

    result, error = backlog.commit_admission(
        task["id"],
        backlog.task_fingerprint(task),
        contract=contract,
        admission=admission,
        transition="claim",
    )

    assert error == ""
    assert result.attempts_before == 0
    assert result.task["status"] == "in_progress"
    assert result.task["attempts"] == 1


def test_attach_claim_session_does_not_increment_attempts():
    task = backlog.add("補 session")
    contract, admission = _payload()
    result, _ = backlog.commit_admission(
        task["id"],
        backlog.task_fingerprint(task),
        contract=contract,
        admission=admission,
        transition="claim",
    )

    current, error = backlog.attach_claim_session(task["id"], "ap123")

    assert error == ""
    assert current["session_id"] == "ap123"
    assert current["attempts"] == result.task["attempts"] == 1


def test_commit_admission_rejects_stale_fingerprint_without_partial_write():
    task = backlog.add("CAS 衝突")
    fingerprint = backlog.task_fingerprint(task)
    backlog.annotate(task["id"], "需求已更新")
    contract, admission = _payload()

    result, error = backlog.commit_admission(
        task["id"],
        fingerprint,
        contract=contract,
        admission=admission,
        transition="claim",
    )

    assert result is None
    assert error == "conflict"
    current = backlog.get(task["id"])
    assert current["status"] == "pending"
    assert current["attempts"] == 0
    assert "admission" not in current


@pytest.mark.asyncio
async def test_two_admission_claims_cannot_claim_the_same_snapshot():
    task = backlog.add("唯一認領")
    fingerprint = backlog.task_fingerprint(task)
    contract, admission = _payload()

    async def claim():
        return await asyncio.to_thread(
            backlog.commit_admission,
            task["id"],
            fingerprint,
            contract=contract,
            admission=admission,
            transition="claim",
        )

    first, second = await asyncio.gather(claim(), claim())
    winners = [result for result, error in (first, second) if result is not None and not error]

    assert len(winners) == 1
    assert backlog.get(task["id"])["attempts"] == 1


@pytest.mark.parametrize(
    ("outcome", "transition", "status"),
    [
        ("needs_clarification", "park", "parked"),
        ("blocked", "park", "parked"),
        ("no_change", "complete", "done"),
    ],
)
def test_non_runnable_admission_transitions_do_not_consume_attempts(outcome, transition, status):
    task = backlog.add(f"結果 {outcome}")
    contract, admission = _payload(outcome)

    result, error = backlog.commit_admission(
        task["id"],
        backlog.task_fingerprint(task),
        contract=contract,
        admission=admission,
        transition=transition,
    )

    assert error == ""
    assert result.task["status"] == status
    assert result.task["attempts"] == 0


@pytest.mark.parametrize("mode", ["off", "shadow"])
def test_non_enforcing_mode_releases_only_enforce_admission_holds(mode):
    held = backlog.add(
        "准入停放",
        admission={
            "mode": "enforce",
            "outcome": "blocked",
            "reasons": ["risk_not_authorized"],
        },
    )
    backlog.set_status(held["id"], "parked")
    manual = backlog.add("人工歸檔")
    backlog.set_status(manual["id"], "parked", note="[手動] 歸檔")

    assert backlog.release_admission_holds(mode=mode) == 1
    assert backlog.get(held["id"])["status"] == "pending"
    assert backlog.get(held["id"])["attempts"] == 0
    assert backlog.get(held["id"])["admission"]["released_by_mode"] == mode
    assert backlog.get(held["id"])["admission"]["original_mode"] == "enforce"
    assert backlog.get(held["id"])["admission"]["mode"] == mode
    assert backlog.get(manual["id"])["status"] == "parked"

    # 若使用者之後重新 park，同一筆 kill-switch 標記不可在下一輪重入。
    backlog.set_status(held["id"], "parked", note="[手動] 再次停放")
    assert backlog.release_admission_holds(mode=mode) == 0
    assert backlog.get(held["id"])["status"] == "parked"
