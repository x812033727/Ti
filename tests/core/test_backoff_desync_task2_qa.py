"""Distributed desync coverage for retry jitter.

N=64 simulates many clients hitting the same attempt at once.  Each test injects
a deterministic serialized rand stream, then requires len(set)>1, pstdev>0, and
all samples inside the exact jitter band.
"""

from __future__ import annotations

from statistics import pstdev

from studio.llm_caller import backoff_delay

CLIENTS = 64
BASE = 2.0
CAP = 60.0
JITTER = 0.5
EPS = 1e-12


def _serialized_rand_values() -> list[float]:
    return [i / (CLIENTS - 1) for i in range(CLIENTS)]


def _assert_desynced_band(delays: list[float], *, lower: float, upper: float) -> None:
    assert len(delays) >= 50
    assert len(set(delays)) > 1
    assert pstdev(delays) > 0.0
    assert all(lower - EPS <= delay <= upper + EPS for delay in delays)


def test_desync_jitter_429_retry_after_path_spreads_upward() -> None:
    retry_after = 10.0
    attempt = 3
    nominal = min(retry_after, CAP)

    delays = [
        backoff_delay(
            retry_after,
            attempt,
            base=BASE,
            cap=CAP,
            jitter=JITTER,
            rand=lambda value=value: value,
        )
        for value in _serialized_rand_values()
    ]

    _assert_desynced_band(delays, lower=nominal, upper=nominal * (1.0 + JITTER))
    assert min(delays) == nominal


def test_desync_jitter_529_without_retry_after_spreads_downward() -> None:
    attempt = 2
    nominal = min(BASE * (2**attempt), CAP)

    delays = [
        backoff_delay(
            None,
            attempt,
            base=BASE,
            cap=CAP,
            jitter=JITTER,
            rand=lambda value=value: value,
        )
        for value in _serialized_rand_values()
    ]

    _assert_desynced_band(delays, lower=nominal * (1.0 - JITTER), upper=nominal)
    assert max(delays) == nominal
