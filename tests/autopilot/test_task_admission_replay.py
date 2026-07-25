"""50 筆去敏任務契約 replay 與 promotion metrics 守門。"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

from studio.task_admission import evaluate

_FIXTURE = Path(__file__).parents[1] / "fixtures" / "task_admission_replay_v1.json"
_OUTCOMES = {
    "ready",
    "investigation",
    "needs_clarification",
    "no_change",
    "blocked",
}
_FORBIDDEN_KEYS = {
    "prompt",
    "raw_prompt",
    "full_output",
    "raw_output",
    "file_content",
    "file_contents",
    "command_output",
    "environment",
    "env",
    "password",
    "secret",
    "api_key",
    "token",
}
_SECRET_VALUE = re.compile(
    r"(?i)(?:"
    r"gh[pousr]_[A-Za-z0-9_]{8,}|"
    r"github_pat_[A-Za-z0-9_]+|"
    r"sk-[A-Za-z0-9_-]{8,}|"
    r"AKIA[A-Z0-9]{12,}|"
    r"BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY|"
    r"(?:password|secret|api[_-]?key|token)\s*[:=]\s*\S+"
    r")"
)


def _load_fixture() -> dict[str, Any]:
    return json.loads(_FIXTURE.read_text(encoding="utf-8"))


def _render(value: Any, variant: int) -> Any:
    """展開合成變體；回傳新物件，fixture template 保持唯讀。"""
    if isinstance(value, dict):
        return {key: _render(item, variant) for key, item in value.items()}
    if isinstance(value, list):
        return [_render(item, variant) for item in value]
    if isinstance(value, str):
        return value.replace("{variant}", str(variant))
    return value


def _expanded_cases(fixture: dict[str, Any]) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for row in fixture["records"]:
        template = _render(fixture["templates"][row["template"]], row["variant"])
        template["task"]["id"] = row["task_id"]
        cases.append({**template, "case_id": row["case_id"], "variant": row["variant"]})
    return cases


def _assert_sanitized(value: Any, path: str = "$") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = key.casefold().replace("-", "_")
            assert normalized not in _FORBIDDEN_KEYS, f"{path}.{key} 不得出現在 replay"
            _assert_sanitized(item, f"{path}.{key}")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _assert_sanitized(item, f"{path}[{index}]")
        return
    if isinstance(value, str):
        assert not _SECRET_VALUE.search(value), f"{path} 疑似含秘密"
        assert "/root/" not in value and "/home/" not in value, f"{path} 不得含本機絕對路徑"
        assert len(value) <= 500, f"{path} 疑似保存完整輸出或檔案內容"


def test_replay_fixture_is_fifty_sanitized_cases_with_the_agreed_mix():
    fixture = _load_fixture()

    assert fixture["schema_version"] == 1
    assert fixture["sanitized"] is True
    assert len(fixture["templates"]) == 10
    assert len(fixture["records"]) == 50
    assert len({row["case_id"] for row in fixture["records"]}) == 50
    _assert_sanitized(fixture)

    cases = _expanded_cases(fixture)
    expected_mix = {"bug": 4, "feature": 3, "test-refactor": 2, "ops": 1}
    for variant in range(1, 6):
        batch = [case for case in cases if case["variant"] == variant]
        assert len(batch) == 10
        assert Counter(case["category"] for case in batch) == expected_mix

    assert Counter(case["category"] for case in cases) == {
        "bug": 20,
        "feature": 15,
        "test-refactor": 10,
        "ops": 5,
    }


def test_replay_covers_all_outcomes_and_produces_promotion_metrics():
    fixture = _load_fixture()
    cases = _expanded_cases(fixture)
    observations: list[tuple[dict[str, Any], Any]] = []

    for case in cases:
        decision = evaluate(case["task"], case["context"], "replay")
        observations.append((case, decision))
        assert decision.outcome == case["expected_outcome"], case["case_id"]
        assert decision.audit["engine"] == "deterministic"

    observed_outcomes = Counter(decision.outcome for _, decision in observations)
    assert set(observed_outcomes) == _OUTCOMES
    assert observed_outcomes == {
        "ready": 15,
        "investigation": 5,
        "needs_clarification": 10,
        "no_change": 10,
        "blocked": 10,
    }

    expected_ready = sum(case["expected_outcome"] == "ready" for case, _ in observations)
    observed_ready = sum(decision.outcome == "ready" for _, decision in observations)
    true_ready = sum(
        case["expected_outcome"] == "ready" and decision.outcome == "ready"
        for case, decision in observations
    )
    exact_matches = sum(
        case["expected_outcome"] == decision.outcome for case, decision in observations
    )
    deterministic = sum(decision.audit["engine"] == "deterministic" for _, decision in observations)
    unsafe_releases = sum(
        case.get("safety_critical") is True and decision.outcome == "ready"
        for case, decision in observations
    )
    metrics = {
        "sample_size": len(observations),
        "ready_precision": true_ready / observed_ready,
        "ready_recall": true_ready / expected_ready,
        "exact_agreement": exact_matches / len(observations),
        "no_llm_rate": deterministic / len(observations),
        "unsafe_release_count": unsafe_releases,
    }

    thresholds = fixture["promotion_thresholds"]
    assert metrics["sample_size"] == 50
    assert metrics["ready_precision"] >= thresholds["ready_precision_min"]
    assert metrics["ready_recall"] >= thresholds["ready_recall_min"]
    assert metrics["exact_agreement"] >= thresholds["exact_agreement_min"]
    assert metrics["no_llm_rate"] >= thresholds["no_llm_rate_min"]
    assert metrics["unsafe_release_count"] <= thresholds["unsafe_release_max"]
