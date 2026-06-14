"""任務 #1：`llm_caller.RetryConfig` 統一退避設定入口的單元測試。

對齊驗收標準與 DECISIONS.md：
- 暴露單一 `RetryConfig`，含 `max_retries／base_delay／cap／jitter` 四欄位。
- `__post_init__` 型別轉換（非數值 → TypeError/ValueError）→ 有限性守門（inf/nan → ValueError）
  → 固定順序夾值（max_retries → base_delay → cap 依賴夾正後 base）→ jitter 超界報錯。
- jitter=0.0 為確定值反向黑樣本；jitter>0 注入固定 rand 驗退避上下界。

全程純計算、無真實網路 I/O、無 sleep（退避測試只算秒數不等待）。
"""

from __future__ import annotations

import pytest

from studio import llm_caller as lc
from studio.llm_caller import RetryConfig


# ── 1. 四欄位存在且預設等價舊行為 ──────────────────────────────────────────
def test_fields_exist_and_defaults():
    cfg = RetryConfig()
    for f in ("max_retries", "base_delay", "cap", "jitter"):
        assert hasattr(cfg, f), f"缺少欄位 {f}"
    # 預設值對齊 module 常數，jitter=0 等價舊行為。
    assert cfg.max_retries == 3
    assert cfg.base_delay == lc.DEFAULT_BACKOFF_BASE == 2.0
    assert cfg.cap == lc.DEFAULT_BACKOFF_CAP == 60.0
    assert cfg.jitter == lc.DEFAULT_BACKOFF_JITTER == 0.0


def test_explicit_valid_values_preserved():
    cfg = RetryConfig(max_retries=5, base_delay=1.5, cap=30.0, jitter=0.5)
    assert (cfg.max_retries, cfg.base_delay, cfg.cap, cfg.jitter) == (5, 1.5, 30.0, 0.5)


# ── 2. 夾值：負 max_retries → 0 ────────────────────────────────────────────
@pytest.mark.parametrize("v,expect", [(-1, 0), (-100, 0), (0, 0), (7, 7)])
def test_max_retries_clamped_nonneg(v, expect):
    assert RetryConfig(max_retries=v).max_retries == expect


# ── 3. 夾值：base_delay 夾為正數 ───────────────────────────────────────────
@pytest.mark.parametrize("v", [0.0, -5.0, -1e-3])
def test_base_delay_clamped_positive(v):
    cfg = RetryConfig(base_delay=v, cap=100.0)
    assert cfg.base_delay == pytest.approx(1e-9)


# ── 4. 夾值順序：cap 依賴夾正後 base ───────────────────────────────────────
def test_cap_clamped_to_base():
    cfg = RetryConfig(base_delay=10.0, cap=5.0)
    assert cfg.cap == 10.0  # cap 不得低於 base


def test_cap_order_depends_on_clamped_base():
    # base 原為負被夾為 1e-9；cap 給更小負值應抬到夾正後的 base，而非原始負值。
    cfg = RetryConfig(base_delay=-3.0, cap=-9.0)
    assert cfg.base_delay == pytest.approx(1e-9)
    assert cfg.cap == pytest.approx(1e-9)
    assert cfg.cap >= cfg.base_delay


# ── 5. jitter 邊界：[0,1] 內合法、外報錯（不靜默夾值） ─────────────────────
@pytest.mark.parametrize("j", [0.0, 0.3, 1.0])
def test_jitter_in_range_ok(j):
    assert RetryConfig(jitter=j).jitter == j


@pytest.mark.parametrize("j", [-0.01, 1.01, 2.0, -1.0])
def test_jitter_out_of_range_raises(j):
    with pytest.raises(ValueError):
        RetryConfig(jitter=j)


# ── 6. 有限性守門：inf/nan 一律報錯（資安：防 sleep(inf) hang / NaN 繞過邊界）─
@pytest.mark.parametrize("bad", [float("inf"), float("-inf")])
def test_base_delay_inf_raises(bad):
    with pytest.raises(ValueError):
        RetryConfig(base_delay=bad)


@pytest.mark.parametrize("bad", [float("inf"), float("-inf")])
def test_cap_inf_raises(bad):
    with pytest.raises(ValueError):
        RetryConfig(cap=bad)


def test_jitter_nan_raises():
    # NaN 任何比較皆 False；`not (0<=j<=1)` 寫法確保 NaN 被攔下而非靜默成最大抖動。
    with pytest.raises(ValueError):
        RetryConfig(jitter=float("nan"))


def test_base_delay_nan_raises():
    with pytest.raises(ValueError):
        RetryConfig(base_delay=float("nan"))


# ── 7. 型別驗證：非數值輸入在建構時即報錯 ──────────────────────────────────
@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_retries": "abc"},
        {"base_delay": "x"},
        {"cap": "y"},
        {"jitter": "z"},
        {"max_retries": None},
        {"base_delay": object()},
    ],
)
def test_non_numeric_raises(kwargs):
    with pytest.raises((TypeError, ValueError)):
        RetryConfig(**kwargs)


def test_numeric_string_coerced():
    # 純數字字串可被 float/int 轉換——驗證型別轉換確實發生。
    cfg = RetryConfig(max_retries="4", base_delay="2.0", cap="20", jitter="0.5")
    assert (cfg.max_retries, cfg.base_delay, cfg.cap, cfg.jitter) == (4, 2.0, 20.0, 0.5)
    assert isinstance(cfg.max_retries, int)
    assert isinstance(cfg.base_delay, float)
    assert isinstance(cfg.cap, float)


# ── 8. RetryConfig 欄位驅動退避：jitter=0 確定值黑樣本 vs jitter>0 上下界 ───
# 任務 #1 尚未整合 backoff_delay(cfg=...)（屬任務 #3），此處以 RetryConfig 欄位直接
# 餵 backoff_delay 的純量參數，驗證「同一組設定」在 jitter=0／jitter>0 下的退避語意。
def test_jitter_zero_is_deterministic_blackbox():
    """反向黑樣本：jitter=0.0 → 退避為確定值，與舊行為等價（rand 不被取用）。"""
    cfg = RetryConfig(max_retries=3, base_delay=2.0, cap=60.0, jitter=0.0)

    def _boom():  # jitter=0 時不應觸碰 rand
        raise AssertionError("jitter=0 不應取用隨機源")

    # 指數退避路徑（無 retry_after）：nominal = min(base*2**attempt, cap)，確定值。
    for attempt, expect in [(0, 2.0), (1, 4.0), (2, 8.0), (5, 60.0)]:
        got = lc.backoff_delay(
            None, attempt, base=cfg.base_delay, cap=cfg.cap, jitter=cfg.jitter, rand=_boom
        )
        assert got == expect, (attempt, got, expect)
    # 429 路徑（retry_after 為主）：jitter=0 直接回 min(retry_after, cap)。
    assert (
        lc.backoff_delay(5.0, 0, base=cfg.base_delay, cap=cfg.cap, jitter=cfg.jitter, rand=_boom)
        == 5.0
    )
    assert (
        lc.backoff_delay(99.0, 0, base=cfg.base_delay, cap=cfg.cap, jitter=cfg.jitter, rand=_boom)
        == 60.0
    )


@pytest.mark.parametrize("r", [0.0, 0.5, 1.0])
def test_jitter_positive_bounds_with_fixed_rand(r):
    """正向樣本：jitter>0 注入固定 rand，退避落點須落在規格上下界。"""
    cfg = RetryConfig(max_retries=3, base_delay=2.0, cap=60.0, jitter=0.5)
    nominal = min(cfg.base_delay * (2**1), cfg.cap)  # attempt=1 → 4.0

    # 指數退避（equal-jitter 向下散開）：落點 = nominal*(1 - jitter*rand) ∈ [nominal*(1-j), nominal]。
    got = lc.backoff_delay(
        None, 1, base=cfg.base_delay, cap=cfg.cap, jitter=cfg.jitter, rand=lambda: r
    )
    assert got == pytest.approx(nominal * (1.0 - cfg.jitter * r))
    assert nominal * (1.0 - cfg.jitter) - 1e-9 <= got <= nominal + 1e-9

    # 429 路徑（jitter 僅向上、夾 cap）：落點 ∈ [min(ra,cap), min(min(ra,cap)*(1+j), cap)]。
    ra_nominal = min(10.0, cfg.cap)
    got_ra = lc.backoff_delay(
        10.0, 0, base=cfg.base_delay, cap=cfg.cap, jitter=cfg.jitter, rand=lambda: r
    )
    assert got_ra == pytest.approx(min(ra_nominal * (1.0 + cfg.jitter * r), cfg.cap))
    assert ra_nominal - 1e-9 <= got_ra <= min(ra_nominal * (1.0 + cfg.jitter), cfg.cap) + 1e-9
