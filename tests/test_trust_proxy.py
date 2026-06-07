"""可信代理 / 來源 IP 設定與解析測試。

任務 #1 範圍（本檔主驗收）：studio/config.py 的 TI_TRUST_PROXY、TI_TRUSTED_PROXIES
與 trust_proxy_enabled()，比照 auth_enabled() 風格。

netutil（client_ip/is_loopback，任務 #2/#3/#5）若尚未交付則自動 skip，待落地後本檔即覆蓋。
"""

from __future__ import annotations

import importlib
import ipaddress

import pytest

from studio import config


@pytest.fixture
def reload_config(monkeypatch):
    """以指定環境變數 reload config，teardown 還原為預設並清快取。

    回傳一個 setter(**env)：設定 env → reload(config) → 回傳 reloaded module。
    """

    def _set(**env):
        for k, v in env.items():
            if v is None:
                monkeypatch.delenv(k, raising=False)
            else:
                monkeypatch.setenv(k, v)
        importlib.reload(config)
        return config

    yield _set
    # 還原：清掉測試 env 後 reload 回預設，避免污染其它測試
    for k in ("TI_TRUST_PROXY", "TI_TRUSTED_PROXIES"):
        monkeypatch.delenv(k, raising=False)
    importlib.reload(config)
    config.reset_trusted_proxies()


# --- 驗收標準 1：預設關閉、向後相容 ------------------------------------
def test_trust_proxy_default_disabled():
    """未設定任何 env 時，門禁預設關閉（沿用 auth_enabled() 的 opt-in 風格）。"""
    # 直接讀現役模組（測試啟動環境未設 TI_TRUST_PROXY）
    assert config.trust_proxy_enabled() is False


def test_trust_proxy_enabled_returns_bool():
    """trust_proxy_enabled() 回傳真正的 bool，型別比照 auth_enabled()。"""
    assert isinstance(config.trust_proxy_enabled(), bool)
    assert isinstance(config.auth_enabled(), bool)


# --- 驗收標準 1 / opt-in：各種開關值解析 -------------------------------
@pytest.mark.parametrize("val", ["1", "true", "True", "yes", "on"])
def test_trust_proxy_truthy_values_enable(reload_config, val):
    cfg = reload_config(TI_TRUST_PROXY=val)
    assert cfg.trust_proxy_enabled() is True


@pytest.mark.parametrize("val", ["0", "false", "False", ""])
def test_trust_proxy_falsy_values_disable(reload_config, val):
    """沿用 not in ("0","false","False","")，這些值一律視為關閉。"""
    cfg = reload_config(TI_TRUST_PROXY=val)
    assert cfg.trust_proxy_enabled() is False


def test_trust_proxy_unset_disabled(reload_config):
    cfg = reload_config(TI_TRUST_PROXY=None)
    assert cfg.trust_proxy_enabled() is False


# --- 驗收標準 5/6：TI_TRUSTED_PROXIES 預設 loopback、支援 IP/CIDR -------
def test_trusted_proxies_default_is_loopback(reload_config):
    """預設僅含 loopback（127.0.0.0/8 與 ::1），涵蓋 IPv4 與 IPv6。"""
    cfg = reload_config(TI_TRUSTED_PROXIES=None)
    nets = cfg.trusted_proxies()
    strs = {str(n) for n in nets}
    assert strs == {"127.0.0.0/8", "::1/128"}
    # 預設清單應判定 loopback 為受信
    assert any(ipaddress.ip_address("127.0.0.1") in n for n in nets)
    assert any(ipaddress.ip_address("::1") in n for n in nets)


def test_trusted_proxies_parses_ip_and_cidr(reload_config):
    cfg = reload_config(TI_TRUSTED_PROXIES="10.0.0.5, 192.168.0.0/16, ::1")
    nets = cfg.trusted_proxies()
    # 單一 IP 被視為 /32 網段
    assert any(ipaddress.ip_address("10.0.0.5") in n for n in nets)
    # CIDR 網段內位址命中
    assert any(ipaddress.ip_address("192.168.42.7") in n for n in nets)
    # 網段外位址不命中
    assert not any(ipaddress.ip_address("8.8.8.8") in n for n in nets)


# --- 驗收標準 6：格式錯誤值「不靜默全信任」---------------------------
def test_trusted_proxies_invalid_item_skipped_not_trust_all(reload_config):
    """含垃圾項時略過該項，絕不退化為 0.0.0.0/0（信任全部）。"""
    cfg = reload_config(TI_TRUSTED_PROXIES="garbage, 10.0.0.1, not-an-ip/99")
    nets = cfg.trusted_proxies()
    strs = {str(n) for n in nets}
    # 僅有效項保留
    assert strs == {"10.0.0.1/32"}
    # 絕不包含「信任全部」網段
    assert "0.0.0.0/0" not in strs
    assert not any(ipaddress.ip_address("8.8.8.8") in n for n in nets)


def test_trusted_proxies_all_invalid_yields_empty_not_trust_all(reload_config):
    """全部無效 → 空清單（誰都不受信），而非退化為信任全部。"""
    cfg = reload_config(TI_TRUSTED_PROXIES="foo, bar/baz")
    nets = cfg.trusted_proxies()
    assert nets == []
    assert not any(ipaddress.ip_address("8.8.8.8") in n for n in nets)


# --- 快取與 reset 行為 -------------------------------------------------
def test_trusted_proxies_cached():
    """同一次設定下重複呼叫回傳同一份快取物件。"""
    config.reset_trusted_proxies()
    first = config.trusted_proxies()
    second = config.trusted_proxies()
    assert first is second


def test_reset_trusted_proxies_reparses(monkeypatch):
    """reset 後重讀 TI_TRUSTED_PROXIES 環境變數，供測試切換。"""
    monkeypatch.setenv("TI_TRUSTED_PROXIES", "172.16.0.0/12")
    config.reset_trusted_proxies()
    try:
        nets = config.trusted_proxies()
        assert any(ipaddress.ip_address("172.16.5.5") in n for n in nets)
    finally:
        monkeypatch.delenv("TI_TRUSTED_PROXIES", raising=False)
        config.reset_trusted_proxies()


# ======================================================================
# netutil（任務 #2/#3/#5）：尚未交付則僅此測試 skip，不影響上方 config 驗收。
# ======================================================================
def test_netutil_api_present_when_delivered():
    netutil = pytest.importorskip(
        "studio.netutil", reason="studio/netutil.py 尚未交付（任務 #2/#3）"
    )
    assert hasattr(netutil, "client_ip")
    assert hasattr(netutil, "is_loopback")
