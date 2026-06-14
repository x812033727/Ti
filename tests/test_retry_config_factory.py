"""任務 #1 驗收：`RetryConfig` dataclass 與 `make_retry_config()` 工廠（experts.py）。

落點說明：架構決策推翻原「llm_caller.py」落點，改放 `experts.py`（理由：llm_caller
明文禁讀 config 以維持 provider 無關）。故本測試對 `experts.make_retry_config`。

驗收標準（#1）：
- `make_retry_config()` 存在、回傳 `RetryConfig`，四欄位分別等於
  `config.EXPERT_RATE_LIMIT_RETRIES / _BACKOFF / _BACKOFF_CAP / _BACKOFF_JITTER`。
- 為 call-time 讀取：monkeypatch config 後「再呼叫」即時反映（非 import 期 cache）。
全程純函式、無網路 I/O。
"""

from __future__ import annotations

import dataclasses

from studio import config, experts
from studio.experts import RetryConfig, make_retry_config


# --- 存在性與型別 -------------------------------------------------------


def test_retry_config_is_frozen_dataclass_with_four_fields():
    assert dataclasses.is_dataclass(RetryConfig)
    field_names = [f.name for f in dataclasses.fields(RetryConfig)]
    assert field_names == ["max_retries", "base", "cap", "jitter"]
    # frozen：寫入欄位應拋例外（值物件不可變，避免共享後被竄改）。
    inst = RetryConfig(max_retries=1, base=2.0, cap=3.0, jitter=0.4)
    try:
        inst.max_retries = 9  # type: ignore[misc]
    except dataclasses.FrozenInstanceError:
        pass
    else:
        raise AssertionError("RetryConfig 應為 frozen dataclass")


def test_make_retry_config_returns_retryconfig():
    cfg = make_retry_config()
    assert isinstance(cfg, RetryConfig)


# --- 四欄位等於現行 config 取值 ----------------------------------------


def test_fields_equal_current_config_constants():
    cfg = make_retry_config()
    assert cfg.max_retries == max(0, config.EXPERT_RATE_LIMIT_RETRIES)
    assert cfg.base == config.EXPERT_RATE_LIMIT_BACKOFF
    assert cfg.cap == config.EXPERT_RATE_LIMIT_BACKOFF_CAP
    assert cfg.jitter == config.EXPERT_RATE_LIMIT_BACKOFF_JITTER


# --- call-time 讀取（核心驗收）：monkeypatch config 後再呼叫即時反映 ----


def test_factory_reads_config_at_call_time(monkeypatch):
    # 先取一份預設，確認與改後值不同（反向對照，證明真的有變）。
    before = make_retry_config()

    monkeypatch.setattr(config, "EXPERT_RATE_LIMIT_RETRIES", 7)
    monkeypatch.setattr(config, "EXPERT_RATE_LIMIT_BACKOFF", 9.5)
    monkeypatch.setattr(config, "EXPERT_RATE_LIMIT_BACKOFF_CAP", 123.0)
    monkeypatch.setattr(config, "EXPERT_RATE_LIMIT_BACKOFF_JITTER", 0.25)

    after = make_retry_config()

    # call-time：改 config 後「下一次呼叫」即時反映，非 import 期釘死的 cache。
    assert after.max_retries == 7
    assert after.base == 9.5
    assert after.cap == 123.0
    assert after.jitter == 0.25
    # 反向對照：至少一欄與改前不同，證明值真的隨 config 走（排除假綠）。
    assert (before.max_retries, before.base, before.cap, before.jitter) != (
        after.max_retries,
        after.base,
        after.cap,
        after.jitter,
    )


def test_negative_retries_clamped_to_zero(monkeypatch):
    # config 取值若為負，工廠以 max(0, ...) 夾為 0（與既有 _speak_with_retries 行為等價）。
    monkeypatch.setattr(config, "EXPERT_RATE_LIMIT_RETRIES", -5)
    assert make_retry_config().max_retries == 0


# --- 工廠由 experts module 持有（防未來繞過直讀 config）-----------------


def test_factory_and_type_exported_from_experts():
    assert hasattr(experts, "make_retry_config")
    assert hasattr(experts, "RetryConfig")
