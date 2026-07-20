"""跨 provider 高風險審查：可信 provider 身分、相同內容與嚴格 verdict。"""

from __future__ import annotations

import hashlib
import json

import pytest

from studio import autonomy_review, config


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


@pytest.mark.asyncio
async def test_two_distinct_providers_review_exact_same_diff_and_evidence(monkeypatch, tmp_path):
    diff, evidence = "diff --git a/x b/x\n+safe", "lint:pass\ntest:pass"
    diff_sha, evidence_sha = _sha(diff), _sha(evidence)
    calls = []
    monkeypatch.setattr(config, "provider_ready", lambda provider=None: True)

    async def fake_complete(system, user, **kwargs):
        calls.append({"system": system, "user": user, **kwargs})
        return json.dumps(
            {
                "verdict": "approve",
                "rationale": "diff 有界、測試完整且 rollback 可行",
                "diff_sha": diff_sha,
                "evidence_sha": evidence_sha,
            }
        )

    monkeypatch.setattr(autonomy_review.providers, "complete_once", fake_complete)
    rows = await autonomy_review.review(
        cwd=tmp_path,
        diff_text=diff,
        evidence_text=evidence,
        diff_sha=diff_sha,
        evidence_sha=evidence_sha,
        session_id="r1",
        provider_pair=("claude", "codex"),
    )
    assert [row["provider"] for row in rows] == ["claude", "codex"]
    assert all(row["verdict"] == "approve" for row in rows)
    assert calls[0]["user"] == calls[1]["user"]
    assert {call["provider"] for call in calls} == {"claude", "codex"}


@pytest.mark.asyncio
async def test_unavailable_or_invalid_reviewer_escalates(monkeypatch, tmp_path):
    diff, evidence = "d", "e"
    diff_sha, evidence_sha = _sha(diff), _sha(evidence)
    monkeypatch.setattr(config, "provider_ready", lambda provider=None: provider == "claude")

    async def malformed(*args, **kwargs):
        return '{"verdict":"approve"}'

    monkeypatch.setattr(autonomy_review.providers, "complete_once", malformed)
    rows = await autonomy_review.review(
        cwd=tmp_path,
        diff_text=diff,
        evidence_text=evidence,
        diff_sha=diff_sha,
        evidence_sha=evidence_sha,
        session_id="r2",
        provider_pair=("claude", "codex"),
    )
    assert rows[0]["verdict"] == "escalate"
    assert rows[0]["rationale"] == "invalid_verdict_schema"
    assert rows[1]["rationale"] == "provider_unavailable"


@pytest.mark.asyncio
async def test_same_provider_or_input_hash_mismatch_never_calls_model(monkeypatch, tmp_path):
    calls = []

    async def should_not_call(*args, **kwargs):
        calls.append(1)
        return ""

    monkeypatch.setattr(autonomy_review.providers, "complete_once", should_not_call)
    same = await autonomy_review.review(
        cwd=tmp_path,
        diff_text="d",
        evidence_text="e",
        diff_sha=_sha("d"),
        evidence_sha=_sha("e"),
        session_id="r3",
        provider_pair=("claude", "claude"),
    )
    assert all(row["verdict"] == "escalate" for row in same)
    mismatch = await autonomy_review.review(
        cwd=tmp_path,
        diff_text="d",
        evidence_text="e",
        diff_sha="wrong",
        evidence_sha=_sha("e"),
        session_id="r4",
        provider_pair=("claude", "codex"),
    )
    assert all(row["rationale"] == "input_hash_mismatch" for row in mismatch)
    assert calls == []


def test_model_cannot_self_assert_provider_or_add_fields():
    diff_sha, evidence_sha = _sha("d"), _sha("e")
    text = json.dumps(
        {
            "provider": "different-provider",
            "verdict": "approve",
            "rationale": "trust me",
            "diff_sha": diff_sha,
            "evidence_sha": evidence_sha,
        }
    )
    row = autonomy_review.parse_verdict(text, "claude", diff_sha, evidence_sha)
    assert row["provider"] == "claude"
    assert row["verdict"] == "escalate" and row["rationale"] == "invalid_verdict_schema"
