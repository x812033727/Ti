from __future__ import annotations

import argparse
import json
import os

import pytest

from scripts import measure_prompt_cache_ab as ab
from studio import events


async def test_prompt_cache_ab_success_path_writes_two_numeric_rows(tmp_path, monkeypatch):
    payloads = [
        {
            "ttft_s": 1.234,
            "cache_read": 0,
            "cache_write": 333,
            "duration_ms": 4200,
            "prompt_tokens": 1000,
            "completion_tokens": 50,
            "cost_usd": 0.01,
        },
        {
            "ttft_s": 0.456,
            "cache_read": 900,
            "cache_write": 0,
            "duration_ms": 1300,
            "prompt_tokens": 1000,
            "completion_tokens": 25,
            "cost_usd": 0.006,
        },
    ]
    calls: list[dict[str, object]] = []

    class FakeExpert:
        def __init__(self, role, session_id, cwd, *, model=None):  # noqa: ANN001
            self.index = len(calls)
            self.session_id = session_id
            self.model = model or ""
            calls.append(
                {
                    "role": role.key,
                    "session_id": session_id,
                    "cwd": str(cwd),
                    "model": model,
                    "disable_prompt_caching": os.environ.get("DISABLE_PROMPT_CACHING"),
                }
            )

        async def speak(self, prompt, broadcast):  # noqa: ANN001
            payload = payloads[self.index]
            await broadcast(
                events.token_usage(
                    self.session_id,
                    "engineer",
                    "claude",
                    self.model,
                    payload["prompt_tokens"],
                    payload["completion_tokens"],
                    payload["prompt_tokens"] + payload["completion_tokens"],
                    cost_usd=payload["cost_usd"],
                    duration_ms=payload["duration_ms"],
                    ttft_s=payload["ttft_s"],
                    cache_read=payload["cache_read"],
                    cache_write=payload["cache_write"],
                )
            )
            return "prompt-cache-ab-ok"

        async def stop(self):
            return None

    monkeypatch.setattr(ab, "Expert", FakeExpert)
    monkeypatch.delenv("DISABLE_PROMPT_CACHING", raising=False)
    monkeypatch.setattr(ab.config, "TURN_IDLE_TIMEOUT", ab.config.TURN_IDLE_TIMEOUT)
    monkeypatch.setattr(ab.config, "TURN_HARD_TIMEOUT", ab.config.TURN_HARD_TIMEOUT)
    monkeypatch.setattr(ab.config, "MAX_TURNS_PER_TURN", ab.config.MAX_TURNS_PER_TURN)

    args = argparse.Namespace(
        role="engineer",
        model="claude-sonnet-4-6",
        prompt="固定同段需求",
        prompt_file=None,
        cwd=str(tmp_path / "workdir"),
        session_id="prompt-cache-ab-test",
        after_attempts=2,
        turn_timeout=1.0,
        max_turns=1,
    )

    payload = await ab._run(args)
    report_path = tmp_path / "report.md"
    raw_path = tmp_path / "latest.json"
    ab._write_outputs(report_path, raw_path, payload)

    markdown = report_path.read_text(encoding="utf-8")
    raw = json.loads(raw_path.read_text(encoding="utf-8"))

    assert payload["real_api"] is True
    assert payload["effort"] == "agent_sdk_default"
    assert len(payload["after_runs"]) == 1
    assert raw["before"]["ttft_s"] == 1.234
    assert raw["before"]["cache_read_input_tokens"] == 0
    assert raw["after"]["ttft_s"] == 0.456
    assert raw["after"]["cache_read_input_tokens"] == 900
    assert "| before | 1 | 1.234 | 0 | 333 | 4200 | 1000 | 50 |" in markdown
    assert "| after | unset | 0.456 | 900 | 0 | 1300 | 1000 | 25 |" in markdown
    assert "after 命中證據：PASS (`cache_read_input_tokens=900`)" in markdown
    assert "after - before：`-0.778` 秒" in markdown

    assert calls[0]["disable_prompt_caching"] == "1"
    assert calls[1]["disable_prompt_caching"] is None
    assert {call["model"] for call in calls} == {"claude-sonnet-4-6"}


def test_prompt_cache_ab_rejects_zero_after_attempts():
    with pytest.raises(SystemExit):
        ab._parser().parse_args(["--after-attempts", "0"])
