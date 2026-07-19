#!/usr/bin/env python3
"""Layer 3 autopilot liveness predicate.

This file intentionally has no dependency on the monitored application runtime.
Keep `liveness_verdict_copy` behavior aligned with the in-repo reference predicate.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

DEFAULT_STATUS_FILE = "/opt/ti/autopilot/status.json"
DEFAULT_STALE_THRESHOLD_S = 300.0
MIN_STALE_THRESHOLD_S = 300.0


def _number_or_none(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    return None


def _str_or_none(value: object) -> str | None:
    if isinstance(value, str) and value:
        return value
    return None


def _dict_or_none(value: object) -> dict | None:
    return value if isinstance(value, dict) else None


_LIVENESS_SLEEP_STATES = frozenset({"quota_sleep", "budget_sleep", "rotate_restart"})


def liveness_verdict_copy(
    status: dict,
    *,
    now: float,
    stale_threshold_s: float,
) -> str:
    state = _str_or_none(status.get("state"))

    if state in _LIVENESS_SLEEP_STATES:
        sleep_until = _number_or_none(status.get("sleep_until"))
        if sleep_until is not None and now < sleep_until + stale_threshold_s:
            return "alive"
        return "dead_main_loop"

    updated_at = _number_or_none(status.get("updated_at"))
    if updated_at is None or now - updated_at > stale_threshold_s:
        return "dead_main_loop"

    if state != "running":
        return "alive"

    workers = _dict_or_none(status.get("workers")) or {}
    cpu_active = workers.get("cpu_active")
    last_activity_at = _number_or_none(status.get("last_activity_at"))
    activity_stale = last_activity_at is None or now - last_activity_at > stale_threshold_s

    if cpu_active is True or not activity_stale:
        return "alive"
    return "dead_task"


def _clean_token(value: object) -> str:
    text = str(value)
    return "".join(ch if ch.isalnum() or ch in "._=-/" else "_" for ch in text)[:200]


def _age(now: float, value: object) -> str:
    number = _number_or_none(value)
    if number is None:
        return "null"
    return str(int(max(0.0, now - number)))


def _cpu_token(workers: object) -> str:
    worker_dict = _dict_or_none(workers) or {}
    value = worker_dict.get("cpu_active")
    if value is True:
        return "true"
    if value is False:
        return "false"
    return "null"


def _probe_fail(reason: str) -> tuple[str, str]:
    return "probe_fail", f"verdict=probe_fail reason={_clean_token(reason)}"


def _snapshot_has_no_liveness_fields(status: dict) -> bool:
    workers = _dict_or_none(status.get("workers"))
    return (
        _str_or_none(status.get("state")) is None
        and _number_or_none(status.get("sleep_until")) is None
        and _number_or_none(status.get("updated_at")) is None
        and _number_or_none(status.get("last_activity_at")) is None
        and workers is None
    )


def evaluate_status(
    status: object,
    *,
    now: float,
    stale_threshold_s: float,
) -> tuple[str, str]:
    try:
        if not isinstance(status, dict):
            return _probe_fail("status_not_object")
        if _snapshot_has_no_liveness_fields(status):
            return _probe_fail("status_has_no_liveness_fields")
        verdict = liveness_verdict_copy(status, now=now, stale_threshold_s=stale_threshold_s)
        state = _str_or_none(status.get("state")) or "null"
        workers = _dict_or_none(status.get("workers")) or {}
        line = " ".join(
            [
                f"verdict={verdict}",
                f"state={_clean_token(state)}",
                f"updated_age_s={_age(now, status.get('updated_at'))}",
                f"last_activity_age_s={_age(now, status.get('last_activity_at'))}",
                f"cpu_active={_cpu_token(workers)}",
            ]
        )
        return verdict, line
    except Exception as exc:  # pragma: no cover - defensive boundary for deployment use
        return _probe_fail(f"exception_{type(exc).__name__}")


def evaluate_file(
    status_file: str,
    *,
    now: float,
    stale_threshold_s: float,
) -> tuple[str, str]:
    try:
        raw = Path(status_file).read_text(encoding="utf-8")
        status = json.loads(raw)
    except FileNotFoundError:
        return _probe_fail("status_file_missing")
    except Exception as exc:
        return _probe_fail(f"status_read_failed_{type(exc).__name__}")
    return evaluate_status(status, now=now, stale_threshold_s=stale_threshold_s)


def _self_test() -> int:
    now = 1_800_000_000.0
    threshold = DEFAULT_STALE_THRESHOLD_S
    fresh = now - 5.0
    stale = now - 3600.0
    samples: list[tuple[str, dict[str, Any], str]] = [
        (
            "white_long_turn_cpu_active",
            {
                "state": "running",
                "updated_at": fresh,
                "last_activity_at": stale,
                "current_expert": "senior",
                "turn_started_at": stale,
                "workers": {"count": 5, "cpu_active": True},
            },
            "alive",
        ),
        (
            "black_cpu_idle_and_activity_stale",
            {
                "state": "running",
                "updated_at": fresh,
                "last_activity_at": stale,
                "current_expert": "senior",
                "turn_started_at": stale,
                "workers": {"count": 5, "cpu_active": False},
            },
            "dead_task",
        ),
    ]
    failed = False
    for name, status, expected in samples:
        got = liveness_verdict_copy(status, now=now, stale_threshold_s=threshold)
        print(f"self-test: {name}: got={got} expected={expected}")
        failed = failed or got != expected
    if failed:
        print("self-test: failed", file=sys.stderr)
        return 1
    print("self-test: ok")
    return 0


def _threshold(raw: float) -> float:
    if raw < MIN_STALE_THRESHOLD_S:
        return MIN_STALE_THRESHOLD_S
    return raw


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument(
        "--status-file",
        default=os.environ.get("TI_LAYER3_STATUS_FILE", DEFAULT_STATUS_FILE),
    )
    parser.add_argument(
        "--stale-threshold-s",
        type=float,
        default=_env_float("TI_LAYER3_STALE_THRESHOLD_S", DEFAULT_STALE_THRESHOLD_S),
    )
    parser.add_argument("--now", type=float, default=None)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    if args.self_test:
        return _self_test()
    now = time.time() if args.now is None else args.now
    threshold = _threshold(args.stale_threshold_s)
    verdict, line = evaluate_file(args.status_file, now=now, stale_threshold_s=threshold)
    print(line)
    return 2 if verdict in {"dead_main_loop", "dead_task"} else 0


if __name__ == "__main__":
    raise SystemExit(main())
