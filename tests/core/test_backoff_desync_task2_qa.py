"""Distributed desync coverage for retry jitter.

N=64 simulates many clients hitting the same attempt at once.  Each test injects
a deterministic serialized rand stream, then requires len(set)>1, pstdev>0, and
all samples inside the exact jitter band.

白/黑樣本對照（任務 #2/#3）：
- 白樣本（jitter=0.5）：同 attempt 的 N 客戶端延遲**去同步**——非全等、pstdev>0、落在理論帶內。
- 黑樣本（jitter=0）：同 attempt 的 N 客戶端延遲**退化為全等**（len(set)==1、pstdev==0）。
  這證明白樣本的「非全等」不是假綠——一旦 jitter 被關掉，測試會立刻抓到退化。
黑白樣本刻意**同檔對照**，讓「開/關 jitter」的差異在同一處一眼可見。
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


# --- 黑樣本：jitter=0 → 同 attempt 延遲全等（退化），證白樣本非假綠 -----------------


def test_black_sample_jitter_zero_429_path_all_equal() -> None:
    """429 路徑 jitter=0：即使餵入去同步的序列化 rand，N 客戶端延遲仍全等 = nominal。"""
    retry_after = 10.0
    attempt = 3
    nominal = min(retry_after, CAP)

    delays = [
        backoff_delay(
            retry_after,
            attempt,
            base=BASE,
            cap=CAP,
            jitter=0.0,
            rand=lambda value=value: value,
        )
        for value in _serialized_rand_values()
    ]

    assert len(delays) >= 50
    assert len(set(delays)) == 1  # 退化：完全同步，無去同步
    assert pstdev(delays) == 0.0
    assert delays[0] == nominal


def test_black_sample_jitter_zero_529_path_all_equal() -> None:
    """529 路徑 jitter=0：無 retry-after 時 N 客戶端延遲仍全等 = 最深退避 nominal。"""
    attempt = 2
    nominal = min(BASE * (2**attempt), CAP)

    delays = [
        backoff_delay(
            None,
            attempt,
            base=BASE,
            cap=CAP,
            jitter=0.0,
            rand=lambda value=value: value,
        )
        for value in _serialized_rand_values()
    ]

    assert len(delays) >= 50
    assert len(set(delays)) == 1  # 退化：完全同步，無去同步
    assert pstdev(delays) == 0.0
    assert delays[0] == nominal
