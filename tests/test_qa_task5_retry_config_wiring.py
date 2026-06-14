"""任務 #5：`RetryConfig` 統一入口的**接線層**回歸測試。

任務 #1～#4 已分別覆蓋 `RetryConfig` 夾值/邊界（test_retryconfig.py）、`backoff_delay`
cfg 屏蔽純量、`run_with_retries` 的 cfg/None 路徑（test_llm_caller.py）、experts 退避行為
（test_experts_ratelimit.py）。本檔補齊**跨模組接線**的缺口，對齊任務 #5 驗收：

1. `config.make_retry_config()` 讀 module 全域（非 `os.getenv`）：`monkeypatch.setattr(config, …)`
   與 `reload()` 都能即時生效；直接 getenv 會繞過 monkeypatch 造成假綠——本檔以「env 設離譜值、
   全域才是真實來源」反向證偽。
2. `reload()` 確實一併重置 `EXPERT_RATE_LIMIT_JITTER`。
3. experts **確實走統一入口**：jitter 由 config 一路流經 `make_retry_config → backoff_delay`。
   以「jitter=0 確定值反向黑樣本」對照「jitter>0 注入固定 rand 驗上下界」兩組佐證——
   改 config 的 jitter 即改 experts 退避行為，只有真的走統一入口才會成立。
4. `run_with_retries` 兩入口皆缺（cfg=None 且 max_retries=None）→ ValueError（任務 #5 第③組）。

全程純計算：所有 config 屬性異動一律 `monkeypatch.setattr`，rand 以固定值注入，sleep 不被觸發，
無真實網路 I/O。
"""

from __future__ import annotations

import pytest

from studio import config, experts, llm_caller


# ── 1. make_retry_config 讀 module 全域、即時反映 monkeypatch ─────────────────
def test_make_retry_config_reflects_monkeypatched_globals(monkeypatch):
    monkeypatch.setattr(config, "EXPERT_RATE_LIMIT_RETRIES", 7)
    monkeypatch.setattr(config, "EXPERT_RATE_LIMIT_BACKOFF", 1.5)
    monkeypatch.setattr(config, "EXPERT_RATE_LIMIT_BACKOFF_CAP", 30.0)
    monkeypatch.setattr(config, "EXPERT_RATE_LIMIT_JITTER", 0.4)

    cfg = config.make_retry_config()

    assert isinstance(cfg, llm_caller.RetryConfig)
    assert (cfg.max_retries, cfg.base_delay, cfg.cap, cfg.jitter) == (7, 1.5, 30.0, 0.4)


def test_make_retry_config_uses_global_not_getenv(monkeypatch):
    """反向證偽假綠：env 設離譜值，但全域（monkeypatch）才是真實來源。

    若工廠誤用 `os.getenv` 會讀到 0.99，monkeypatch 形同無效——此測即會紅。
    """
    monkeypatch.setenv("TI_RATELIMIT_JITTER", "0.99")  # 直接 getenv 才會讀到的離譜值
    monkeypatch.setattr(config, "EXPERT_RATE_LIMIT_JITTER", 0.2)  # 全域＝唯一真實來源

    assert config.make_retry_config().jitter == 0.2  # 取全域、非 env → 0.2 而非 0.99


# ── 2. reload 一併重置 jitter ────────────────────────────────────────────────
def test_reload_resets_jitter_from_env(monkeypatch):
    monkeypatch.setattr(config, "EXPERT_RATE_LIMIT_JITTER", 0.9)
    monkeypatch.setenv("TI_RATELIMIT_JITTER", "0.25")
    config.reload()
    try:
        assert config.EXPERT_RATE_LIMIT_JITTER == 0.25  # reload 確實刷新 jitter 全域
    finally:
        # 還原為環境預設，避免 reload 改動的全域污染後續測試。
        monkeypatch.delenv("TI_RATELIMIT_JITTER", raising=False)
        config.reload()


def test_reload_jitter_defaults_zero_when_env_absent(monkeypatch):
    """env 缺省時 reload 後 jitter＝0.0（等價舊行為）。"""
    monkeypatch.setattr(config, "EXPERT_RATE_LIMIT_JITTER", 0.7)
    monkeypatch.delenv("TI_RATELIMIT_JITTER", raising=False)
    config.reload()
    try:
        assert config.EXPERT_RATE_LIMIT_JITTER == 0.0
    finally:
        config.reload()


# ── 3. experts 確實走統一入口：jitter 由 config 流經統一退避 ──────────────────
def test_experts_backoff_jitter_zero_is_deterministic_blackbox(monkeypatch):
    """反向黑樣本：config.jitter=0（預設）→ experts 退避為確定值，rand 不被取用。"""
    monkeypatch.setattr(config, "EXPERT_RATE_LIMIT_RETRIES", 2)
    monkeypatch.setattr(config, "EXPERT_RATE_LIMIT_BACKOFF", 2.0)
    monkeypatch.setattr(config, "EXPERT_RATE_LIMIT_BACKOFF_CAP", 60.0)
    monkeypatch.setattr(config, "EXPERT_RATE_LIMIT_JITTER", 0.0)

    def _boom():  # jitter=0 時統一入口不應觸碰隨機源
        raise AssertionError("jitter=0 不應取用 rand")

    monkeypatch.setattr(llm_caller.random, "random", _boom)

    # 529／指數路徑：nominal = min(base*2**attempt, cap)，確定值。
    assert experts._backoff_delay(None, 0) == 2.0
    assert experts._backoff_delay(None, 2) == 8.0
    assert experts._backoff_delay(None, 5) == 60.0  # 夾 cap
    # 429 路徑：retry-after 為主、夾 cap。
    assert experts._backoff_delay(5.0, 0) == 5.0
    assert experts._backoff_delay(99.0, 0) == 60.0


@pytest.mark.parametrize("r", [0.0, 0.5, 1.0])
def test_experts_backoff_jitter_from_config_bounds(monkeypatch, r):
    """正向樣本：config.jitter>0 注入固定 rand → experts 退避落在規格上下界。

    這同時證明 experts 真的走統一入口：唯有 `_backoff_delay → make_retry_config → backoff_delay`
    這條鏈把 config 的 jitter 帶進來，改 config 才會改退避；若 experts 仍硬接 base/cap、未納
    jitter，下方 529 路徑的折減就不會發生（落點恆為 8.0），此測即紅。
    """
    monkeypatch.setattr(config, "EXPERT_RATE_LIMIT_BACKOFF", 2.0)
    monkeypatch.setattr(config, "EXPERT_RATE_LIMIT_BACKOFF_CAP", 60.0)
    monkeypatch.setattr(config, "EXPERT_RATE_LIMIT_JITTER", 0.5)
    monkeypatch.setattr(llm_caller.random, "random", lambda: r)

    # 529／指數（equal-jitter 向下）：nominal=8，落點 = 8*(1 - 0.5*r) ∈ [4, 8]。
    nominal = 8.0
    got = experts._backoff_delay(None, 2)
    assert got == pytest.approx(nominal * (1.0 - 0.5 * r))
    assert nominal * (1.0 - 0.5) - 1e-9 <= got <= nominal + 1e-9

    # 429（jitter 僅向上、夾 cap）：ra=10，落點 = min(10*(1+0.5*r), 60) ∈ [10, 15]。
    ra_nominal = 10.0
    got_ra = experts._backoff_delay(10.0, 0)
    assert got_ra == pytest.approx(min(ra_nominal * (1.0 + 0.5 * r), 60.0))
    assert ra_nominal - 1e-9 <= got_ra <= ra_nominal * 1.5 + 1e-9


def test_experts_backoff_jitter_changes_with_config(monkeypatch):
    """直接對照：同一 (retry_after, attempt) 下，jitter=0 與 jitter>0 給出不同退避，
    證明 jitter 旋鈕經 config 真正生效（非寫死忽略）。"""
    monkeypatch.setattr(config, "EXPERT_RATE_LIMIT_BACKOFF", 2.0)
    monkeypatch.setattr(config, "EXPERT_RATE_LIMIT_BACKOFF_CAP", 60.0)
    monkeypatch.setattr(llm_caller.random, "random", lambda: 1.0)

    monkeypatch.setattr(config, "EXPERT_RATE_LIMIT_JITTER", 0.0)
    deterministic = experts._backoff_delay(None, 3)  # 16.0

    monkeypatch.setattr(config, "EXPERT_RATE_LIMIT_JITTER", 0.5)
    jittered = experts._backoff_delay(None, 3)  # 16*(1-0.5)=8.0

    assert deterministic == 16.0
    assert jittered == pytest.approx(8.0)
    assert jittered != deterministic  # jitter 確實改變了退避


# ── 4. run_with_retries 兩入口皆缺 → ValueError（任務 #5 第③組） ──────────────
async def test_run_with_retries_requires_cfg_or_max_retries():
    async def _attempt():
        return "unreached"

    async def _noop(_snip, _partial):
        return "fallback"

    with pytest.raises(ValueError):
        await llm_caller.run_with_retries(
            _attempt,
            max_retries=None,
            cfg=None,
            on_rate_limit_exhausted=_noop,
            on_api_error=_noop,
        )
