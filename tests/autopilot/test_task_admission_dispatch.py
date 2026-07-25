"""Claim-time admission 協調：lazy migration、enforce 與既有 attempts 語意。"""

from __future__ import annotations

import json

import pytest

from studio import autopilot, backlog, config
from studio.task_admission import (
    claim_next_task,
    claim_next_task_with_semantic_fallback,
    enqueue_items,
    enqueue_task,
)


@pytest.fixture(autouse=True)
def state(tmp_path, monkeypatch):
    state = tmp_path / "state"
    repo = tmp_path / "repo"
    (repo / "studio").mkdir(parents=True)
    (repo / "studio" / "target.py").write_text("", encoding="utf-8")
    monkeypatch.setattr(config, "AUTOPILOT_STATE_DIR", state)
    monkeypatch.setattr(backlog, "_read_cache", {}, raising=False)
    return {"state": state, "repo": repo}


def _ready_contract(kind: str = "implementation") -> dict:
    acceptance = {
        "implementation": ["pytest tests -q", "reviewable diff"],
        "investigation": ["交付結論、證據與是否需改碼"],
    }[kind]
    return {
        "version": 1,
        "outcome": "可客觀驗收的結果",
        "kind": kind,
        "targets": ["studio/target.py"],
        "acceptance": acceptance,
        "constraints": [],
        "external_writes": [],
    }


def _repo_context(state) -> dict:
    return {"root": state["repo"], "repo_sha": "a" * 40}


def test_shadow_lazy_migrates_legacy_task_but_still_claims(state):
    task = backlog.add("改善模糊需求", source="manual", risk="low")

    selected = claim_next_task(mode="shadow", repo_context=_repo_context(state))

    assert selected.task["id"] == task["id"]
    assert selected.task["attempts"] == 0, "runner 必須看到 claim 前 attempts"
    current = backlog.get(task["id"])
    assert current["status"] == "in_progress"
    assert current["attempts"] == 1
    assert current["admission"]["outcome"] == "needs_clarification"
    assert current["admission"]["mode"] == "shadow"
    assert current["contract"]["version"] == 1


def test_stale_real_snapshot_is_still_recognized_as_persisted(state):
    task = backlog.add("可能被旁路先認領的任務", source="manual", risk="low")
    stale_snapshot = dict(task)
    backlog.set_status(task["id"], "in_progress")

    assert autopilot._is_persisted_core_task(stale_snapshot) is True


def test_enforced_requirement_contains_canonical_contract_and_consumed_override(
    state,
    monkeypatch,
):
    monkeypatch.setattr(config, "TASK_ADMISSION_MODE", "enforce")
    task = {
        "title": "改善派工",
        "detail": "原始描述",
        "contract": _ready_contract(),
        "admission": {
            "override": {
                "reason": "採用低風險預設，但保留現有 API",
                "consumed_at": 123.0,
            }
        },
    }

    requirement = autopilot._task_requirement(task)

    assert "已驗證任務契約" in requirement
    assert "studio/target.py" in requirement
    assert "pytest tests -q" in requirement
    assert "reviewable diff" in requirement
    assert "採用低風險預設，但保留現有 API" in requirement
    assert "不代表外部寫入授權" in requirement


def test_enforce_admission_replaces_legacy_async_clarify_probe(state, monkeypatch):
    monkeypatch.setattr(config, "CLARIFY_ASYNC", True)
    task = {
        "title": "已由 admission 補全",
        "attempts": 0,
        "admission": {"outcome": "ready"},
    }

    monkeypatch.setattr(config, "TASK_ADMISSION_MODE", "enforce")
    assert autopilot._should_run_clarify_probe(task) is False

    monkeypatch.setattr(config, "TASK_ADMISSION_MODE", "shadow")
    assert autopilot._should_run_clarify_probe(task) is True


def test_shadow_enqueue_records_admission_without_changing_lifecycle(state):
    task = enqueue_task(
        "新人工需求",
        source="manual",
        risk="low",
        mode="shadow",
        repo_context=_repo_context(state),
    )

    assert task["status"] == "pending"
    assert task["attempts"] == 0
    assert task["admission"]["outcome"] == "needs_clarification"
    assert task["admission"]["phase"] == "enqueue"
    rows = [
        json.loads(line)
        for line in (state["state"] / "admission_audit.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert rows[0]["task_id"] == task["id"]
    assert rows[0]["outcome"] == "needs_clarification"
    assert rows[0]["mode"] == "shadow"
    assert rows[0]["latency_ms"] >= 0
    assert rows[0]["cache_hit"] is False
    assert rows[0]["model"] is None
    assert rows[0]["token_usage"] is None


def test_shadow_internal_evaluator_error_preserves_legacy_dispatch(
    state,
    monkeypatch,
):
    task = backlog.add("shadow 評估器壞掉仍沿用派工", source="manual", risk="low")

    def broken_evaluate(*_args, **_kwargs):
        raise RuntimeError("do not persist this detail")

    monkeypatch.setattr("studio.task_admission.evaluate", broken_evaluate)

    selected = claim_next_task(mode="shadow", repo_context=_repo_context(state))

    assert selected.task["id"] == task["id"]
    current = backlog.get(task["id"])
    assert current["status"] == "in_progress"
    assert current["attempts"] == 1
    assert current["admission"]["outcome"] == "blocked"
    assert current["admission"]["model_error"] == "internal_error"
    assert "do not persist this detail" not in json.dumps(
        current["admission"],
        ensure_ascii=False,
    )


def test_shadow_evaluator_error_conflict_refreshes_before_lower_priority_claim(
    state,
    monkeypatch,
):
    first = backlog.add("優先任務", source="manual", risk="low", priority=0)
    backlog.add("次要任務", source="discovered", risk="low", priority=1)

    def broken_evaluate(*_args, **_kwargs):
        raise RuntimeError("observer failed")

    real_commit = backlog.commit_admission
    conflicted = False

    def conflict_once(task_id, expected_fingerprint, **kwargs):
        nonlocal conflicted
        if task_id == first["id"] and not conflicted:
            conflicted = True
            backlog.annotate(first["id"], "並行更新")
            return None, "conflict"
        return real_commit(task_id, expected_fingerprint, **kwargs)

    monkeypatch.setattr("studio.task_admission.evaluate", broken_evaluate)
    monkeypatch.setattr(backlog, "commit_admission", conflict_once)

    selected = claim_next_task(mode="shadow", repo_context=_repo_context(state))

    assert selected is not None
    assert selected.task["id"] == first["id"]
    assert backlog.get(first["id"])["status"] == "in_progress"


def test_shadow_admission_commit_failure_uses_atomic_legacy_claim(
    state,
    monkeypatch,
):
    task = backlog.add("shadow 寫 admission 失敗仍要認領", source="manual", risk="low")

    monkeypatch.setattr(
        backlog,
        "commit_admission",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("write failed")),
    )

    selected = claim_next_task(mode="shadow", repo_context=_repo_context(state))

    assert selected.task["id"] == task["id"]
    current = backlog.get(task["id"])
    assert current["status"] == "in_progress"
    assert current["attempts"] == 1


@pytest.mark.parametrize("mode", ["shadow", "enforce"])
def test_enqueue_internal_evaluator_error_is_safe_and_observable(
    state,
    monkeypatch,
    mode,
):
    def broken_evaluate(*_args, **_kwargs):
        raise RuntimeError("secret exception text")

    monkeypatch.setattr("studio.task_admission.evaluate", broken_evaluate)

    task = enqueue_task(
        f"{mode} 入列評估器故障",
        source="discovered",
        risk="low",
        mode=mode,
        repo_context=_repo_context(state),
    )

    assert task["status"] == "pending"
    assert task["attempts"] == 0
    assert task["admission"]["outcome"] == "blocked"
    assert task["admission"]["model_error"] == "internal_error"
    assert "secret exception text" not in json.dumps(task["admission"], ensure_ascii=False)
    if mode == "enforce":
        assert task["retry_after"] > 0


def test_enforce_enqueue_fail_closed_without_consuming_attempt(state):
    task = enqueue_task(
        "模糊人工需求",
        source="manual",
        risk="low",
        mode="enforce",
        repo_context=_repo_context(state),
    )

    assert task["status"] == "parked"
    assert task["attempts"] == 0
    assert task["admission"]["needs_human"] is True


def test_successful_enforce_enqueue_resets_unpaused_error_streak(state):
    autopilot.task_admission._record_admission_circuit(
        "internal_error",
        state_dir=None,
    )

    enqueue_task(
        "完整入列任務",
        source="discovered",
        risk="low",
        contract=_ready_contract(),
        mode="enforce",
        repo_context=_repo_context(state),
    )

    circuit = autopilot.task_admission.admission_circuit_state()
    assert circuit["consecutive_errors"] == 0
    assert circuit["paused"] is False


def test_repeated_enqueue_commit_failures_accumulate_and_trip_circuit(state, monkeypatch):
    monkeypatch.setattr(
        backlog,
        "commit_admission",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("storage unavailable")),
    )

    for index in range(3):
        enqueue_task(
            f"持續寫入失敗 {index}",
            source="discovered",
            risk="low",
            contract=_ready_contract(),
            mode="enforce",
            repo_context=_repo_context(state),
        )

    circuit = autopilot.task_admission.admission_circuit_state()
    assert circuit["consecutive_errors"] == 3
    assert circuit["paused"] is True


def test_repeated_sync_claim_commit_failures_accumulate_and_trip_circuit(state, monkeypatch):
    backlog.add(
        "持續 claim 寫入失敗",
        source="discovered",
        risk="low",
        contract=_ready_contract(),
    )
    monkeypatch.setattr(
        backlog,
        "commit_admission",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("storage unavailable")),
    )

    for _index in range(3):
        assert claim_next_task(mode="enforce", repo_context=_repo_context(state)) is None

    circuit = autopilot.task_admission.admission_circuit_state()
    assert circuit["consecutive_errors"] == 3
    assert circuit["paused"] is True


@pytest.mark.asyncio
async def test_repeated_async_claim_commit_failures_accumulate_and_trip_circuit(
    state,
    monkeypatch,
):
    backlog.add(
        "持續 async claim 寫入失敗",
        source="discovered",
        risk="low",
        contract=_ready_contract(),
    )
    monkeypatch.setattr(
        backlog,
        "commit_admission",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("storage unavailable")),
    )

    for _index in range(3):
        assert (
            await claim_next_task_with_semantic_fallback(
                mode="enforce",
                repo_context=_repo_context(state),
                resolver=None,
                cache_dir=state["state"] / "admission_cache",
            )
            is None
        )

    circuit = autopilot.task_admission.admission_circuit_state()
    assert circuit["consecutive_errors"] == 3
    assert circuit["paused"] is True


def test_enforce_deterministic_claim_requires_known_repo_sha(state):
    task = backlog.add(
        "完整契約仍須綁定 repo SHA",
        source="discovered",
        risk="low",
        contract=_ready_contract(),
    )

    selected = claim_next_task(
        mode="enforce",
        repo_context={"root": state["repo"]},
    )

    assert selected is None
    current = backlog.get(task["id"])
    assert current["status"] == "pending"
    assert current["attempts"] == 0
    assert current["retry_after"] > 0
    assert current["admission"]["reasons"] == ["admission_internal_error"]


@pytest.mark.asyncio
async def test_production_shadow_claim_uses_semantic_coordinator(state, monkeypatch):
    monkeypatch.setattr(config, "TASK_ADMISSION_MODE", "shadow")
    captured = {}

    async def fake_semantic_claim(**kwargs):
        captured.update(kwargs)
        return None

    monkeypatch.setattr(
        autopilot.task_admission,
        "claim_next_task_with_semantic_fallback",
        fake_semantic_claim,
    )

    assert await autopilot._claim_next_with_admission() is None
    assert captured["mode"] == "shadow"
    assert captured["resolver"] is autopilot._resolve_admission_contract


@pytest.mark.asyncio
async def test_enforce_runner_pins_clone_to_claim_admission_sha(state, monkeypatch):
    monkeypatch.setattr(config, "TASK_ADMISSION_MODE", "enforce")
    task = backlog.add(
        "執行版本必須與准入版本一致",
        source="discovered",
        risk="low",
        contract=_ready_contract(),
    )
    selected = claim_next_task(mode="enforce", repo_context=_repo_context(state))
    assert selected is not None and selected.task["id"] == task["id"]
    expected_sha = selected.task["admission"]["audit"]["repo_sha"]
    captured = {}

    class StopAfterClone(BaseException):
        pass

    async def fake_prepare_clone(*, repo_sha=None):
        captured["repo_sha"] = repo_sha
        raise StopAfterClone

    monkeypatch.setattr(autopilot, "_prepare_clone", fake_prepare_clone)

    with pytest.raises(StopAfterClone):
        await autopilot.run_one_task({**selected.task, "_admission_claimed": True})
    assert captured["repo_sha"] == expected_sha == "a" * 40


@pytest.mark.asyncio
async def test_prepare_clone_verifies_and_resets_to_requested_sha(state, monkeypatch):
    work = state["state"] / "work"
    (work / ".git").mkdir(parents=True)
    expected_sha = "b" * 40
    calls = []

    async def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        if "fetch" in argv:
            return 1, "temporary network failure"
        if argv[:3] == ["git", "rev-parse", "HEAD"]:
            return 0, expected_sha
        return 0, ""

    monkeypatch.setattr(autopilot, "_run", fake_run)
    monkeypatch.setattr(config, "AUTOPILOT_BRANCH", "main")

    assert await autopilot._prepare_clone(str(work), repo_sha=expected_sha) == str(work)
    commands = [argv for argv, _kwargs in calls]
    assert ["git", "cat-file", "-e", f"{expected_sha}^{{commit}}"] in commands
    assert ["git", "reset", "--hard", expected_sha] in commands


@pytest.mark.asyncio
async def test_enforce_pin_failure_requeues_without_consuming_attempt(state, monkeypatch):
    monkeypatch.setattr(config, "TASK_ADMISSION_MODE", "enforce")
    task = backlog.add(
        "無法固定版本時不可執行",
        source="discovered",
        risk="low",
        contract=_ready_contract(),
    )
    selected = claim_next_task(mode="enforce", repo_context=_repo_context(state))
    assert selected is not None

    async def fail_pin(*, repo_sha=None):
        assert repo_sha == "a" * 40
        raise RuntimeError("must not persist this git detail")

    monkeypatch.setattr(autopilot, "_prepare_clone", fail_pin)

    await autopilot.run_one_task({**selected.task, "_admission_claimed": True})
    current = backlog.get(task["id"])
    assert current["status"] == "pending"
    assert current["attempts"] == 0
    assert current["retry_after"] > 0
    assert "must not persist" not in str(current.get("note") or "")
    assert current["admission"]["reasons"] == ["admission_internal_error"]


def test_enforce_generated_clarification_waits_for_claim_time_refinement(state):
    task = enqueue_task(
        "系統產生但仍模糊的需求",
        source="eval",
        risk="low",
        mode="enforce",
        repo_context=_repo_context(state),
    )

    assert task["status"] == "pending"
    assert task["attempts"] == 0
    assert task["admission"]["outcome"] == "needs_clarification"
    assert task["admission"]["needs_human"] is False


def test_off_enqueue_preserves_legacy_shape(state):
    task = enqueue_task(
        "舊流程需求",
        source="manual",
        mode="off",
        repo_context=_repo_context(state),
        contract=_ready_contract(),
    )

    assert task["status"] == "pending"
    assert "admission" not in task
    assert "contract" not in task


def test_generated_batch_is_admitted_but_cannot_self_approve(state):
    count = enqueue_items(
        [
            {
                "title": "完整生成任務",
                "risk": "low",
                "contract": _ready_contract(),
                "human_approved": True,
            }
        ],
        source="eval",
        mode="shadow",
        repo_context=_repo_context(state),
    )

    task = backlog.list_tasks()[0]
    assert count == 1
    assert task["admission"]["outcome"] == "ready"
    assert task.get("human_approved") is not True


def test_enforce_parks_human_clarification_then_claims_next_ready_task(state):
    blocked = backlog.add("模糊人工任務", source="manual", risk="low", priority=0)
    ready = backlog.add(
        "完整自動任務",
        source="discovered",
        risk="low",
        contract=_ready_contract(),
    )

    selected = claim_next_task(mode="enforce", repo_context=_repo_context(state))

    assert selected.task["id"] == ready["id"]
    parked = backlog.get(blocked["id"])
    assert parked["status"] == "parked"
    assert parked["attempts"] == 0
    assert parked["admission"]["needs_human"] is True
    assert backlog.get(ready["id"])["status"] == "in_progress"


def test_enforce_routes_valid_investigation_as_runnable(state):
    task = backlog.add(
        "調查 timeout 根因",
        source="manual",
        risk="low",
        contract=_ready_contract("investigation"),
    )

    selected = claim_next_task(mode="enforce", repo_context=_repo_context(state))

    assert selected.decision.outcome == "investigation"
    assert selected.task["id"] == task["id"]
    assert backlog.get(task["id"])["status"] == "in_progress"


def test_sync_enforce_claim_obeys_paused_circuit(state):
    for _ in range(3):
        autopilot.task_admission._record_admission_circuit(
            "internal_error",
            state_dir=None,
        )
    task = backlog.add(
        "熔斷後不得被同步 API 認領",
        source="manual",
        risk="low",
        contract=_ready_contract(),
    )

    selected = claim_next_task(mode="enforce", repo_context=_repo_context(state))

    assert selected is None
    assert backlog.get(task["id"])["status"] == "pending"
    assert backlog.get(task["id"])["attempts"] == 0


def test_enforce_completes_recent_duplicate_without_attempt(state):
    old = backlog.add("已完成工作", source="manual")
    backlog.set_status(old["id"], "done")
    duplicate = backlog.add(
        "已完成工作",
        source="manual",
        risk="low",
        contract=_ready_contract(),
    )

    selected = claim_next_task(mode="enforce", repo_context=_repo_context(state))

    assert selected is None
    current = backlog.get(duplicate["id"])
    assert current["status"] == "done"
    assert current["attempts"] == 0
    assert current["admission"]["outcome"] == "no_change"


def test_claim_conflict_refreshes_order_before_considering_lower_priority(
    state,
    monkeypatch,
):
    high = backlog.add(
        "高優先但首輪發生 CAS 衝突",
        source="manual",
        risk="low",
        priority=0,
        contract=_ready_contract(),
    )
    backlog.add(
        "低優先不可趁 stale snapshot 插隊",
        source="manual",
        risk="low",
        priority=2,
        contract=_ready_contract(),
    )
    real_commit = backlog.commit_admission
    conflicted = False

    def conflict_once(task_id, expected_fingerprint, **kwargs):
        nonlocal conflicted
        if task_id == high["id"] and not conflicted:
            conflicted = True
            backlog.annotate(task_id, "concurrent metadata update")
            return None, "conflict"
        return real_commit(task_id, expected_fingerprint, **kwargs)

    monkeypatch.setattr(backlog, "commit_admission", conflict_once)

    selected = claim_next_task(
        mode="enforce",
        repo_context=_repo_context(state),
        state_dir=state["state"],
    )

    assert selected is not None
    assert selected.task["id"] == high["id"]


def test_predicate_is_evaluated_outside_backlog_lock(state):
    full = backlog.add(
        "完整實作",
        source="manual",
        risk="low",
        contract=_ready_contract(),
    )
    investigation = backlog.add(
        "調查問題",
        source="manual",
        risk="low",
        contract=_ready_contract("investigation"),
    )

    def only_investigation(task):
        # 若 predicate 被放進 flock，這個 nested read 會死鎖。
        backlog.list_tasks()
        return task["contract"]["kind"] == "investigation"

    selected = claim_next_task(
        mode="enforce",
        repo_context=_repo_context(state),
        predicate=only_investigation,
    )

    assert selected.task["id"] == investigation["id"]
    assert backlog.get(full["id"])["status"] == "pending"


@pytest.mark.asyncio
async def test_enforce_claim_uses_one_semantic_fallback_then_deterministic_evidence(state):
    task = backlog.add("修復排序", source="manual", risk="low", item_type="bug")
    calls = 0

    async def resolver(_payload):
        nonlocal calls
        calls += 1
        return {
            "contract": _ready_contract(),
            "model": "fast-test",
            "token_usage": {"input": 3, "output": 4},
        }

    selected = await claim_next_task_with_semantic_fallback(
        mode="enforce",
        repo_context=_repo_context(state),
        resolver=resolver,
        cache_dir=state["state"] / "admission_cache",
    )

    assert calls == 1
    assert selected.task["id"] == task["id"]
    assert selected.decision.outcome == "ready"
    assert backlog.get(task["id"])["attempts"] == 1


@pytest.mark.asyncio
async def test_sideline_predicate_rechecks_semantic_decision_before_claim(
    state,
    monkeypatch,
):
    monkeypatch.setattr(config, "TASK_ADMISSION_MODE", "enforce")
    monkeypatch.setattr(config, "AUTOPILOT_INVESTIGATION_LANE", True)
    task = backlog.add("調查排序根因", source="discovered", risk="low", item_type="bug")

    async def resolver(_payload):
        return {"contract": _ready_contract("implementation")}

    selected = await claim_next_task_with_semantic_fallback(
        mode="enforce",
        repo_context=_repo_context(state),
        resolver=resolver,
        cache_dir=state["state"] / "admission_cache",
        predicate=autopilot._is_investigation_task,
    )

    assert selected is None
    current = backlog.get(task["id"])
    assert current["status"] == "pending"
    assert current["attempts"] == 0


@pytest.mark.asyncio
async def test_enforce_semantic_failure_defers_without_attempt(state):
    task = backlog.add("模糊任務", source="manual", risk="low", item_type="bug")

    async def resolver(_payload):
        raise RuntimeError("provider unavailable")

    selected = await claim_next_task_with_semantic_fallback(
        mode="enforce",
        repo_context=_repo_context(state),
        resolver=resolver,
        cache_dir=state["state"] / "admission_cache",
    )

    assert selected is None
    current = backlog.get(task["id"])
    assert current["status"] == "pending"
    assert current["attempts"] == 0
    assert current["retry_after"] > 0
    assert current["admission"]["outcome"] == "blocked"
    assert current["admission"]["needs_human"] is False
    assert current["admission"]["model_error"] == "resolver_error"


@pytest.mark.asyncio
async def test_shadow_semantic_failure_observes_but_preserves_legacy_claim(state):
    task = backlog.add("shadow 模糊任務", source="manual", risk="low", item_type="bug")

    async def resolver(_payload):
        raise RuntimeError("provider unavailable")

    selected = await claim_next_task_with_semantic_fallback(
        mode="shadow",
        repo_context=_repo_context(state),
        resolver=resolver,
        cache_dir=state["state"] / "admission_cache",
    )

    assert selected is not None
    assert selected.task["id"] == task["id"]
    assert selected.task["attempts"] == 0
    current = backlog.get(task["id"])
    assert current["status"] == "in_progress"
    assert current["attempts"] == 1
    assert current["admission"]["model_error"] == "resolver_error"
    assert autopilot.task_admission.admission_circuit_state()["consecutive_errors"] == 0


@pytest.mark.asyncio
async def test_enforce_internal_evaluator_error_defers_without_attempt(
    state,
    monkeypatch,
):
    task = backlog.add("評估器例外時不得放行", source="discovered", risk="low")

    def broken_evaluate(*_args, **_kwargs):
        raise RuntimeError("sensitive internal detail")

    monkeypatch.setattr("studio.task_admission.evaluate", broken_evaluate)

    selected = await claim_next_task_with_semantic_fallback(
        mode="enforce",
        repo_context=_repo_context(state),
        resolver=None,
        cache_dir=state["state"] / "admission_cache",
    )

    assert selected is None
    current = backlog.get(task["id"])
    assert current["status"] == "pending"
    assert current["attempts"] == 0
    assert current["retry_after"] > 0
    assert current["admission"]["reasons"] == ["admission_internal_error"]
    assert current["admission"]["model_error"] == "internal_error"
    assert "sensitive internal detail" not in json.dumps(
        current["admission"],
        ensure_ascii=False,
    )


@pytest.mark.asyncio
async def test_circuit_reset_failure_cannot_release_a_ready_task(
    state,
    monkeypatch,
):
    task = backlog.add(
        "circuit 寫入失敗不得放行",
        source="discovered",
        risk="low",
        contract=_ready_contract(),
    )

    monkeypatch.setattr(
        "studio.task_admission._record_admission_circuit",
        lambda _error, *, state_dir: {
            "version": 1,
            "consecutive_errors": 3,
            "paused": True,
            "last_error": "circuit_write_failed",
        },
    )

    selected = await claim_next_task_with_semantic_fallback(
        mode="enforce",
        repo_context=_repo_context(state),
        resolver=None,
        cache_dir=state["state"] / "admission_cache",
    )

    assert selected is None
    current = backlog.get(task["id"])
    assert current["status"] == "pending"
    assert current["attempts"] == 0
    assert current["retry_after"] > 0


@pytest.mark.asyncio
async def test_three_internal_errors_trip_durable_admission_circuit(state):
    async def resolver(_payload):
        raise RuntimeError("provider unavailable")

    for index in range(3):
        backlog.add(
            f"模糊任務 {index}",
            source="discovered",
            risk="low",
            item_type="bug",
        )
        await claim_next_task_with_semantic_fallback(
            mode="enforce",
            repo_context=_repo_context(state),
            resolver=resolver,
            cache_dir=state["state"] / "admission_cache",
        )

    circuit = json.loads((state["state"] / "admission_circuit.json").read_text(encoding="utf-8"))
    assert circuit["consecutive_errors"] == 3
    assert circuit["paused"] is True

    untouched = backlog.add("熔斷後任務", source="manual", risk="low")
    selected = await claim_next_task_with_semantic_fallback(
        mode="enforce",
        repo_context=_repo_context(state),
        resolver=resolver,
        cache_dir=state["state"] / "admission_cache",
    )
    assert selected is None
    assert "admission" not in backlog.get(untouched["id"])


@pytest.mark.asyncio
async def test_claim_that_trips_circuit_cannot_fall_through_to_discovery(state, monkeypatch):
    class StopLoop(BaseException):
        pass

    monkeypatch.setattr(config, "TASK_ADMISSION_MODE", "enforce")
    monkeypatch.setattr(config, "AUTOPILOT_QUOTA_GATE", False)
    monkeypatch.setattr(config, "AUTOPILOT_DRYRUN", True)
    monkeypatch.setattr(config, "autopilot_paused", lambda: False)
    monkeypatch.setattr(autopilot, "_shutdown_requested", False)
    monkeypatch.setattr(autopilot, "_consecutive_fail_pause_active", False)
    monkeypatch.setattr(autopilot, "_loop_tick", lambda: None)
    monkeypatch.setattr(autopilot, "_maybe_apply_pinned_account", lambda: None)
    monkeypatch.setattr(autopilot, "_note_resumed", lambda: None)
    monkeypatch.setattr(autopilot, "_maybe_enqueue_schedules", lambda: None)
    monkeypatch.setattr(autopilot, "_write_status", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(autopilot, "_maybe_triage_failed", lambda: None)
    monkeypatch.setattr(autopilot, "_recover_stale_in_progress", lambda: None)
    monkeypatch.setattr(autopilot, "_maybe_clarify_timeout", lambda: 0)
    monkeypatch.setattr(autopilot.autonomy, "policy_exists", lambda *_args: False)
    monkeypatch.setattr(autopilot.notify, "send_bg", lambda *_args, **_kwargs: None)

    async def no_op(*_args, **_kwargs):
        return None

    for name in (
        "_maybe_triage_timeout_parked",
        "_maybe_norms_distill",
        "_maybe_intent_discovery",
        "_maybe_boundary_redeploy",
        "_maybe_reconcile_open_prs",
    ):
        monkeypatch.setattr(autopilot, name, no_op)

    task = backlog.add("claim 時熔斷", source="discovered", risk="low")

    async def trip_during_claim(*_args, **_kwargs):
        for _index in range(3):
            autopilot.task_admission._record_admission_circuit(
                "internal_error",
                state_dir=None,
            )
        return None

    async def stop_on_pause(_seconds):
        raise StopLoop

    monkeypatch.setattr(autopilot, "_claim_next_with_admission", trip_during_claim)
    monkeypatch.setattr(
        autopilot,
        "_prepare_clone",
        lambda *_args, **_kwargs: pytest.fail("paused 後不得準備 discovery clone"),
    )
    monkeypatch.setattr(
        autopilot,
        "_evaluate_self",
        lambda *_args, **_kwargs: pytest.fail("paused 後不得啟動 discovery"),
    )
    monkeypatch.setattr(autopilot.asyncio, "sleep", stop_on_pause)

    with pytest.raises(StopLoop):
        await autopilot._main_loop(startup_sig=0.0)
    assert backlog.get(task["id"])["status"] == "pending"
    assert autopilot.task_admission.admission_circuit_state()["paused"] is True


@pytest.mark.asyncio
async def test_paused_admission_circuit_stops_main_work_before_discovery(
    state,
    monkeypatch,
):
    statuses = []
    sleeps = []
    notifications = []
    circuit_path = state["state"] / "admission_circuit.json"
    circuit_path.parent.mkdir(parents=True, exist_ok=True)
    circuit_path.write_text(
        json.dumps(
            {
                "version": 1,
                "consecutive_errors": 3,
                "paused": True,
                "notified": False,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "TASK_ADMISSION_MODE", "enforce")
    monkeypatch.setattr(
        autopilot,
        "_write_status",
        lambda state, **fields: statuses.append((state, fields)),
    )

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(autopilot.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(
        autopilot.notify,
        "send_bg",
        lambda *args, **kwargs: notifications.append((args, kwargs)),
    )

    first = await autopilot._maybe_pause_for_admission_circuit()
    second = await autopilot._maybe_pause_for_admission_circuit()

    assert first is second is True
    assert sleeps == [60, 60]
    assert statuses[0][0] == "paused"
    assert statuses[0][1]["quota"]["admission_circuit"]["consecutive_errors"] == 3
    assert len(notifications) == 1
    assert notifications[0][0][0] == "admission_circuit_paused"


@pytest.mark.asyncio
async def test_admission_circuit_notification_start_failure_retries(
    state,
    monkeypatch,
):
    circuit_path = state["state"] / "admission_circuit.json"
    circuit_path.parent.mkdir(parents=True, exist_ok=True)
    circuit_path.write_text(
        json.dumps(
            {
                "version": 1,
                "consecutive_errors": 3,
                "paused": True,
                "notified": False,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "TASK_ADMISSION_MODE", "enforce")
    monkeypatch.setattr(autopilot, "_write_status", lambda *_args, **_kwargs: None)

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(autopilot.asyncio, "sleep", no_sleep)
    calls = 0

    def flaky_send(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("thread start failed")

    monkeypatch.setattr(autopilot.notify, "send_bg", flaky_send)

    assert await autopilot._maybe_pause_for_admission_circuit() is True
    assert await autopilot._maybe_pause_for_admission_circuit() is True
    assert calls == 2


def test_circuit_write_failure_latches_process_fail_closed(state, monkeypatch):
    from studio import secure_write

    real_secure_write = secure_write.secure_write_root
    monkeypatch.setattr(
        secure_write,
        "secure_write_root",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk unavailable")),
    )

    result = autopilot.task_admission._record_admission_circuit(
        "internal_error",
        state_dir=None,
    )
    reread = autopilot.task_admission.admission_circuit_state()

    assert result["paused"] is True
    assert reread["paused"] is True
    assert reread["last_error"] == "circuit_write_failed"

    monkeypatch.setattr(secure_write, "secure_write_root", real_secure_write)
    after_recovery = autopilot.task_admission._record_admission_circuit(
        "another_internal_error",
        state_dir=None,
    )
    assert after_recovery["paused"] is True
    assert after_recovery["consecutive_errors"] == 3


def test_inconsistent_circuit_state_is_fail_closed(state):
    path = state["state"] / "admission_circuit.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "consecutive_errors": 999,
                "paused": False,
            }
        ),
        encoding="utf-8",
    )

    circuit = autopilot.task_admission.admission_circuit_state()

    assert circuit["paused"] is True
    assert circuit["last_error"] == "circuit_state_invalid"
