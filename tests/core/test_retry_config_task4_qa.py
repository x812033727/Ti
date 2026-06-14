"""任務 #4：`RetryConfig` dataclass 結構化入口的測試（QA）。

聚焦於 `__post_init__` 的三件事——自動生成退避、clamp 防線、顯式注入優先——
這些是既有 `backoff_delay`（函式層）與 wiring 測試（call-site 層）都沒覆蓋的縫：
沒有任何既有測試直接 `RetryConfig(...)` 建構並驗證其閉包行為。

排假綠策略：
- 反向黑樣本：`jitter=0` → 自動生成的 backoff 為確定值（不受隨機源影響）；
  `jitter>0` → 注入固定隨機源後落在 jitter 帶內、429 路徑不早於 retry-after。
- clamp 四條邊界各配 `pytest.warns` 斷言「有發 warning 留跡」（非靜默退化），
  並進一步驗證 clamp 後的值真的反映在 backoff 行為上（不只是改了屬性）。
- 不可變性：建構後 mutate 屬性不影響已固化的閉包（證明捕捉本地值、非 self）。
"""

from __future__ import annotations

import warnings

import pytest

from studio import llm_caller as lc
from studio.llm_caller import (
    DEFAULT_BACKOFF_BASE,
    DEFAULT_BACKOFF_CAP,
    DEFAULT_BACKOFF_JITTER,
    RetryConfig,
    backoff_delay,
)

# --- ① 自動生成：backoff is None → __post_init__ 依 base/cap/jitter 建閉包 --------


def test_autogen_backoff_matches_backoff_delay_defaults():
    """預設建構（不傳 base/cap/jitter）的自動退避，與 backoff_delay 預設等價——
    向後相容鐵則：新欄位採預設時行為不變。"""
    cfg = RetryConfig(max_retries=3)
    assert cfg.backoff is not None
    # 429 路徑（retry_after 為主）與 529 路徑（指數）皆對齊 backoff_delay 預設輸出。
    assert cfg.backoff(5.0, 0) == backoff_delay(
        5.0,
        0,
        base=DEFAULT_BACKOFF_BASE,
        cap=DEFAULT_BACKOFF_CAP,
        jitter=DEFAULT_BACKOFF_JITTER,
    )
    assert cfg.backoff(None, 2) == backoff_delay(
        None,
        2,
        base=DEFAULT_BACKOFF_BASE,
        cap=DEFAULT_BACKOFF_CAP,
        jitter=DEFAULT_BACKOFF_JITTER,
    )


def test_autogen_backoff_honors_custom_base_cap():
    """自訂 base/cap 經 __post_init__ 固化進閉包，輸出與同參數 backoff_delay 一致。"""
    cfg = RetryConfig(max_retries=3, base=2.0, cap=10.0)
    assert cfg.backoff(None, 0) == 2.0  # 2×2^0
    assert cfg.backoff(None, 5) == 10.0  # 2×2^5 夾 cap
    assert cfg.backoff(99.0, 0) == 10.0  # retry-after 也夾 cap


# --- ② 反向黑樣本：jitter=0 確定值 vs jitter>0 抖動 ------------------------------


def test_black_sample_jitter_zero_deterministic(monkeypatch):
    """jitter=0：即使把隨機源換成極端值，輸出仍是確定的 nominal（黑樣本）。"""
    # 把全域隨機源換成「絕對不該被呼叫」的爆炸源——jitter=0 時根本不會取用。
    monkeypatch.setattr(lc.random, "random", lambda: 1.0)
    cfg = RetryConfig(max_retries=3, base=2.0, cap=10.0, jitter=0.0)
    assert cfg.backoff(4.0, 0) == 4.0  # 429：nominal=min(4,10)，無抖動
    assert cfg.backoff(None, 2) == 8.0  # 529：2×2^2，無抖動
    # 多次呼叫值不變（確定性）
    assert cfg.backoff(4.0, 0) == cfg.backoff(4.0, 0)


def test_white_sample_jitter_positive_429_not_before_retry_after(monkeypatch):
    """jitter>0 白樣本：429 路徑注入 rand=1.0 → 落在上界，且不早於 retry-after。"""
    monkeypatch.setattr(lc.random, "random", lambda: 1.0)
    cfg = RetryConfig(max_retries=3, base=2.0, cap=60.0, jitter=0.5)
    d = cfg.backoff(4.0, 0)  # nominal=4，jitter 向上：4×(1+0.5×1)=6
    assert d == 6.0
    assert d >= 4.0  # 永不早於伺服器要求的 retry-after


def test_white_sample_jitter_positive_529_jitters_down(monkeypatch):
    """jitter>0 白樣本：529 路徑 equal-jitter 向下散開，rand=1.0 → 落在下界。"""
    monkeypatch.setattr(lc.random, "random", lambda: 1.0)
    cfg = RetryConfig(max_retries=3, base=2.0, cap=60.0, jitter=0.5)
    d = cfg.backoff(None, 2)  # nominal=8，向下：8×(1-0.5×1)=4
    assert d == 4.0


def test_white_sample_jitter_band_over_random_draws():
    """jitter>0：真實隨機源多次抽樣，全部落在 equal-jitter 帶內（排假綠：真的有抖動）。"""
    cfg = RetryConfig(max_retries=3, base=2.0, cap=60.0, jitter=0.5)
    nominal = 8.0  # 2×2^2
    seen = set()
    for _ in range(200):
        d = cfg.backoff(None, 2)
        assert nominal * 0.5 <= d <= nominal  # 帶內
        seen.add(round(d, 6))
    assert len(seen) > 1  # 確有抖動，非退化成單一值（反向排假綠）


# --- ③ clamp 邊界：先 warn 留跡再 silent clamp，且 clamp 反映在行為上 -----------


def test_clamp_cap_non_positive_warns_and_resets():
    with pytest.warns(UserWarning, match="cap"):
        cfg = RetryConfig(max_retries=3, cap=0.0)
    assert cfg.cap == DEFAULT_BACKOFF_CAP
    # 行為驗證：cap 已回預設 60，指數退避大 attempt 夾在 60（非 0、無除零退化）。
    assert cfg.backoff(None, 10) == DEFAULT_BACKOFF_CAP


def test_clamp_base_non_positive_warns_and_resets():
    with pytest.warns(UserWarning, match="base"):
        cfg = RetryConfig(max_retries=3, base=0.0)
    assert cfg.base == DEFAULT_BACKOFF_BASE
    # base=0 會讓 529 路徑產出 0 延遲（thundering herd）；clamp 後 attempt=0 → base 本身。
    assert cfg.backoff(None, 0) == DEFAULT_BACKOFF_BASE


def test_clamp_negative_max_retries_warns_and_resets():
    with pytest.warns(UserWarning, match="max_retries"):
        cfg = RetryConfig(max_retries=-5)
    assert cfg.max_retries == 0


def test_clamp_jitter_above_one_warns_and_clamps():
    with pytest.warns(UserWarning, match="jitter"):
        cfg = RetryConfig(max_retries=3, jitter=1.5)
    assert cfg.jitter == 1.0


def test_clamp_jitter_negative_warns_and_clamps():
    with pytest.warns(UserWarning, match="jitter"):
        cfg = RetryConfig(max_retries=3, jitter=-0.3)
    assert cfg.jitter == 0.0


def test_legal_values_emit_no_warning():
    """反向對照：合法輸入完全不發 warning（避免 warn 噪音／假陽性）。"""
    with warnings.catch_warnings():
        warnings.simplefilter("error")  # 任何 warning 都會變成 error
        cfg = RetryConfig(max_retries=5, base=2.0, cap=30.0, jitter=0.25)
    assert (cfg.max_retries, cfg.base, cfg.cap, cfg.jitter) == (5, 2.0, 30.0, 0.25)


# --- ④ 顯式 backoff 注入優先：__post_init__ 不覆蓋 ------------------------------


def test_explicit_backoff_not_overridden():
    sentinel_calls = []

    def custom(ra, att):
        sentinel_calls.append((ra, att))
        return 42.0

    cfg = RetryConfig(max_retries=3, backoff=custom)
    assert cfg.backoff is custom  # 同一物件，未被自動生成覆蓋
    assert cfg.backoff(1.0, 0) == 42.0
    assert sentinel_calls == [(1.0, 0)]


def test_explicit_backoff_survives_clamp_path():
    """即使同時觸發 clamp（非法 cap），顯式 backoff 仍不被覆蓋（生成在 clamp 之後判 None）。"""
    custom = lambda ra, att: 7.0  # noqa: E731
    with pytest.warns(UserWarning, match="cap"):
        cfg = RetryConfig(max_retries=3, cap=-1.0, backoff=custom)
    assert cfg.backoff is custom
    assert cfg.cap == DEFAULT_BACKOFF_CAP  # clamp 仍有發生


# --- ⑤ 不可變性：建構後 mutate 屬性不影響已固化的閉包 --------------------------


def test_autogen_closure_immune_to_post_mutation():
    """閉包捕捉 clamp 後的本地值（非 self）：建構後改 base/cap/jitter 不改變退避輸出。"""
    cfg = RetryConfig(max_retries=3, base=2.0, cap=10.0, jitter=0.0)
    before = cfg.backoff(None, 2)  # 8.0
    cfg.base = 999.0  # 事後 mutate
    cfg.cap = 999.0
    cfg.jitter = 1.0
    after = cfg.backoff(None, 2)
    assert before == after == 8.0  # 退避輸出不受事後 mutate 影響


# --- ⑥ as_kwargs：展開三參數、預設行為等價舊行為 ------------------------------


def test_as_kwargs_exports_three_params_and_backoff_non_none():
    cfg = RetryConfig(max_retries=4)
    kw = cfg.as_kwargs()
    assert set(kw) == {"max_retries", "backoff", "sleep"}
    assert kw["max_retries"] == 4
    assert kw["backoff"] is cfg.backoff
    assert callable(kw["backoff"])  # __post_init__ 後保證非 None
    assert kw["sleep"] is cfg.sleep


def test_as_kwargs_default_backoff_equivalent_to_legacy():
    """as_kwargs 帶出的預設 backoff，輸出與直接呼叫 backoff_delay 預設一致（零回歸鐵證）。"""
    cfg = RetryConfig(max_retries=3)
    bk = cfg.as_kwargs()["backoff"]
    for ra, att in [(5.0, 0), (None, 0), (None, 3), (120.0, 0)]:
        assert bk(ra, att) == backoff_delay(
            ra,
            att,
            base=DEFAULT_BACKOFF_BASE,
            cap=DEFAULT_BACKOFF_CAP,
            jitter=DEFAULT_BACKOFF_JITTER,
        )
