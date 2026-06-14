"""QA 驗證：任務 #2 — experts.make_retry_config() 工廠。

驗收對象（僅 #2 範圍：工廠本身，不含 #3 _speak_with_retries 接線）：
- make_retry_config() 存在於 experts.py，回傳 llm_caller.RetryConfig。
- call-time 讀 config 四值；monkeypatch config 後重呼，回傳值即時反映（lazy）。
- backoff 為 lazy 引用（_backoff_delay），被呼叫時才讀 BACKOFF/_CAP/_JITTER。
- max_retries 在工廠端 clamp ≥0（防呆在最近端）。
- as_kwargs() 展開為 run_with_retries 可吃的三鍵字典。
"""

import asyncio

import pytest

from studio import config, experts, llm_caller

# ---- 存在性 / 型別 / 結構 ----------------------------------------------------


def test_factory_exists_and_returns_retryconfig():
    cfg = experts.make_retry_config()
    assert isinstance(cfg, llm_caller.RetryConfig)
    # backoff/sleep 直接引用模組級 lazy 函式（設計決策：不另建 closure）
    assert cfg.backoff is experts._backoff_delay
    assert cfg.sleep is experts._sleep
    assert callable(cfg.backoff)
    assert callable(cfg.sleep)


def test_as_kwargs_shape_matches_run_with_retries():
    kw = experts.make_retry_config().as_kwargs()
    assert set(kw) == {"max_retries", "backoff", "sleep"}
    # 三鍵須能被 run_with_retries 的簽章接受（不真跑網路）
    import inspect

    sig = inspect.signature(llm_caller.run_with_retries)
    for k in kw:
        assert k in sig.parameters, f"{k} 不在 run_with_retries 簽章"


# ---- max_retries: call-time 讀 config + lazy 反向對照 ------------------------


def test_max_retries_reads_config_at_call_time(monkeypatch):
    monkeypatch.setattr(config, "EXPERT_RATE_LIMIT_RETRIES", 3)
    assert experts.make_retry_config().max_retries == 3
    # 反向對照：改 config 值後「重呼」工廠即時反映 → 證 call-time、非 import 快照
    monkeypatch.setattr(config, "EXPERT_RATE_LIMIT_RETRIES", 7)
    assert experts.make_retry_config().max_retries == 7


def test_max_retries_clamped_to_zero(monkeypatch):
    monkeypatch.setattr(config, "EXPERT_RATE_LIMIT_RETRIES", -5)
    assert experts.make_retry_config().max_retries == 0


def test_max_retries_zero_passthrough(monkeypatch):
    monkeypatch.setattr(config, "EXPERT_RATE_LIMIT_RETRIES", 0)
    assert experts.make_retry_config().max_retries == 0


# ---- backoff: lazy closure（被呼叫時才讀 config） ---------------------------


def test_backoff_is_lazy_reads_config_when_invoked(monkeypatch):
    """關鍵 lazy 證明：工廠回傳 backoff 後再改 config，呼叫 backoff 仍反映新值。"""
    monkeypatch.setattr(config, "EXPERT_RATE_LIMIT_BACKOFF", 2.0)
    monkeypatch.setattr(config, "EXPERT_RATE_LIMIT_BACKOFF_CAP", 60.0)
    monkeypatch.setattr(config, "EXPERT_RATE_LIMIT_BACKOFF_JITTER", 0.0)

    cfg = experts.make_retry_config()  # 先取得物件
    assert cfg.backoff(None, 0) == 2.0  # base 2 × 2^0

    # 取得 cfg 之後才改 config → 若是快照會仍為舊值；lazy 則反映新值
    monkeypatch.setattr(config, "EXPERT_RATE_LIMIT_BACKOFF", 5.0)
    assert cfg.backoff(None, 0) == 5.0  # 即時反映新 base

    # cap 夾擠也在呼叫時讀
    monkeypatch.setattr(config, "EXPERT_RATE_LIMIT_BACKOFF_CAP", 3.0)
    assert cfg.backoff(None, 0) == 3.0


def test_backoff_jitter_read_at_call_time(monkeypatch):
    # 固定 rand=1.0 取 jitter 邊界（沿用 test_experts_ratelimit 範式）
    monkeypatch.setattr(llm_caller.random, "random", lambda: 1.0)
    monkeypatch.setattr(config, "EXPERT_RATE_LIMIT_BACKOFF", 4.0)
    monkeypatch.setattr(config, "EXPERT_RATE_LIMIT_BACKOFF_CAP", 60.0)
    monkeypatch.setattr(config, "EXPERT_RATE_LIMIT_BACKOFF_JITTER", 0.0)
    cfg = experts.make_retry_config()
    assert cfg.backoff(None, 0) == 4.0
    # 取得 cfg 後改 jitter（指數路徑向下散開）→ 即時反映 → 證 call-time 讀 jitter
    monkeypatch.setattr(config, "EXPERT_RATE_LIMIT_BACKOFF_JITTER", 0.5)
    assert cfg.backoff(None, 0) == 4.0 * (1 - 0.5)  # 2.0


# ---- 反向假綠對照：證 lazy 確實有別於快照 -----------------------------------


def test_negative_control_distinguishes_lazy_from_snapshot(monkeypatch):
    """若實作誤把 config 值快照進 closure，下列斷言會抓出（lazy 才會通過）。"""
    monkeypatch.setattr(config, "EXPERT_RATE_LIMIT_RETRIES", 1)
    monkeypatch.setattr(config, "EXPERT_RATE_LIMIT_BACKOFF", 1.0)
    monkeypatch.setattr(config, "EXPERT_RATE_LIMIT_BACKOFF_CAP", 60.0)
    monkeypatch.setattr(config, "EXPERT_RATE_LIMIT_BACKOFF_JITTER", 0.0)
    cfg = experts.make_retry_config()

    # backoff 是 lazy：取得 cfg 後改值，呼叫即時反映（快照實作會回 1.0 → fail）
    monkeypatch.setattr(config, "EXPERT_RATE_LIMIT_BACKOFF", 9.0)
    assert cfg.backoff(None, 0) == 9.0

    # max_retries 是工廠呼叫時讀的純量（已 clamp）：對「同一 cfg」不會回溯改值，
    # 但「重呼工廠」會反映 → 兩者都驗，確認語義正確
    assert cfg.max_retries == 1  # 同一物件保留取得時的快照（純量本就如此）
    monkeypatch.setattr(config, "EXPERT_RATE_LIMIT_RETRIES", 4)
    assert experts.make_retry_config().max_retries == 4  # 重呼反映新值


# ---- sleep 可被注入且為 awaitable（測試零等待縫） ---------------------------


def test_sleep_is_awaitable_no_real_wait():
    cfg = experts.make_retry_config()
    # sleep(0) 不應真的等待；驗證為協程且可完成
    asyncio.run(cfg.sleep(0))


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
