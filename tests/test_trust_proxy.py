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
# netutil.client_ip（任務 #2）：XFF「由右往左跳過受信代理、取最右非受信」解析。
# ======================================================================
from fastapi import Request  # noqa: E402

from studio import netutil  # noqa: E402


def make_request(peer=None, xff=None):
    """建構真實 Starlette Request。

    peer: socket peer host 字串；None → scope 不含 client（client is None）。
    xff:  None / 字串 / list[字串]；多個字串 → 多筆同名 X-Forwarded-For header。
    """
    scope = {"type": "http", "headers": []}
    if peer is not None:
        scope["client"] = (peer, 12345)
    if xff is not None:
        values = [xff] if isinstance(xff, str) else list(xff)
        scope["headers"] = [(b"x-forwarded-for", v.encode()) for v in values]
    return Request(scope)


@pytest.fixture
def proxy(monkeypatch):
    """切換 TI_TRUST_PROXY 與受信代理清單；teardown 自動還原。"""

    def _setup(enabled, proxies="127.0.0.0/8,::1"):
        monkeypatch.setattr(config, "TRUST_PROXY", enabled)
        monkeypatch.setenv("TI_TRUSTED_PROXIES", proxies)
        config.reset_trusted_proxies()

    yield _setup
    monkeypatch.delenv("TI_TRUSTED_PROXIES", raising=False)
    config.reset_trusted_proxies()


def test_netutil_api_present():
    assert callable(netutil.client_ip)
    assert callable(netutil.is_loopback)


# --- 入口 guard：peer 不可知 → None -----------------------------------
def test_client_ip_peer_none_returns_none(proxy):
    proxy(True)
    assert netutil.client_ip(make_request(peer=None, xff="1.2.3.4")) is None


# --- 驗收標準 1：trust 關閉 → 完全忽略 XFF、只認 socket peer -----------
def test_client_ip_trust_off_ignores_xff(proxy):
    proxy(False)
    req = make_request(peer="127.0.0.1", xff="203.0.113.9, 8.8.8.8")
    assert netutil.client_ip(req) == "127.0.0.1"


def test_client_ip_trust_off_no_xff_returns_peer(proxy):
    proxy(False)
    assert netutil.client_ip(make_request(peer="198.51.100.7")) == "198.51.100.7"


# --- 驗收標準 2：受信代理 → 由右往左跳過受信、取最右非受信，不採信最左 --
def test_client_ip_single_hop(proxy):
    proxy(True)  # 受信清單預設含 127.0.0.0/8
    req = make_request(peer="127.0.0.1", xff="203.0.113.9")
    assert netutil.client_ip(req) == "203.0.113.9"


def test_client_ip_multi_hop_skips_trusted_not_leftmost(proxy):
    """最左是偽造值，正解是被代理鏈包住的最右非受信位址。"""
    proxy(True, proxies="127.0.0.0/8,::1,10.0.0.0/8")
    # 鏈：[偽造 1.2.3.4] , [真實 203.0.113.9] , [內部代理 10.1.1.1] ; peer=127.0.0.1(受信)
    req = make_request(peer="127.0.0.1", xff="1.2.3.4, 203.0.113.9, 10.1.1.1")
    got = netutil.client_ip(req)
    assert got == "203.0.113.9"
    assert got != "1.2.3.4"  # 絕不採信最左值


def test_client_ip_merges_multiple_xff_headers(proxy):
    """多筆同名 X-Forwarded-For header 合併後再由右往左解析。"""
    proxy(True, proxies="127.0.0.0/8,::1,10.0.0.0/8")
    req = make_request(
        peer="127.0.0.1",
        xff=["1.2.3.4", "203.0.113.9, 10.1.1.1"],  # 合併 = 1.2.3.4,203.0.113.9,10.1.1.1
    )
    assert netutil.client_ip(req) == "203.0.113.9"


# --- 驗收標準 3（client_ip 層）：最左塞 127.0.0.1 不被冒充取出 --------
def test_client_ip_malicious_leftmost_loopback_not_returned(proxy):
    proxy(True, proxies="10.0.0.0/8")
    req = make_request(peer="10.0.0.1", xff="127.0.0.1, 203.0.113.9")
    got = netutil.client_ip(req)
    assert got == "203.0.113.9"
    assert got != "127.0.0.1"


# --- 驗收標準 4：來源非受信代理 → 即使帶 XFF 也以 socket peer 為準 -----
def test_client_ip_untrusted_peer_ignores_xff(proxy):
    proxy(True)  # 受信僅 loopback
    req = make_request(peer="8.8.8.8", xff="203.0.113.9")
    assert netutil.client_ip(req) == "8.8.8.8"


# --- fail-safe：斷鏈止點（右側出現無法解析段）→ 回退 socket peer ------
def test_client_ip_garbage_breaks_chain_fallback_peer(proxy):
    proxy(True)
    req = make_request(peer="127.0.0.1", xff="203.0.113.9, garbage")
    # 由右往左：先遇 "garbage" 無法解析 → fail-safe 回退 peer
    assert netutil.client_ip(req) == "127.0.0.1"


def test_client_ip_trailing_comma_not_break_point(proxy):
    """尾隨逗號/空白產生的純空段須被跳過、不可當斷鏈止點，否則外部來源誤回退成 loopback peer。"""
    proxy(True)
    req = make_request(peer="127.0.0.1", xff="203.0.113.7, ")
    assert netutil.client_ip(req) == "203.0.113.7"
    # 安全核心：此外部請求不得被誤判為本機。
    assert netutil.is_loopback(req) is False


def test_client_ip_consecutive_commas_skipped(proxy):
    """連續逗號產生的空段同樣跳過，仍取真實 client。"""
    proxy(True)
    req = make_request(peer="127.0.0.1", xff="203.0.113.7,,")
    assert netutil.client_ip(req) == "203.0.113.7"


def test_client_ip_all_trusted_chain_fallback_peer(proxy):
    """全鏈皆受信代理（掃完仍無非受信）→ 回退 socket peer。"""
    proxy(True)  # 受信含 127.0.0.0/8 與 ::1
    req = make_request(peer="127.0.0.1", xff="127.0.0.1, ::1")
    assert netutil.client_ip(req) == "127.0.0.1"


def test_client_ip_trusted_peer_no_xff_returns_peer(proxy):
    proxy(True)
    assert netutil.client_ip(make_request(peer="127.0.0.1")) == "127.0.0.1"


# --- port / IPv6 zone 剝離 --------------------------------------------
def test_client_ip_strips_ipv4_port(proxy):
    proxy(True)
    req = make_request(peer="127.0.0.1", xff="203.0.113.9:5678")
    assert netutil.client_ip(req) == "203.0.113.9"


def test_client_ip_strips_ipv6_bracket_port(proxy):
    proxy(True)
    req = make_request(peer="127.0.0.1", xff="[2001:db8::1]:443")
    assert netutil.client_ip(req) == "2001:db8::1"


def test_client_ip_strips_ipv6_zone(proxy):
    proxy(True)
    req = make_request(peer="127.0.0.1", xff="fe80::1%eth0")
    assert netutil.client_ip(req) == "fe80::1"


# ======================================================================
# netutil.is_loopback（任務 #3）：建在 client_ip 上、用 ipaddress.is_loopback、fail-closed。
# ======================================================================
def test_is_loopback_ipv4_peer(proxy):
    """trust 關閉時 peer=127.0.0.1 → loopback True（涵蓋 127.0.0.0/8）。"""
    proxy(False)
    assert netutil.is_loopback(make_request(peer="127.0.0.1")) is True


def test_is_loopback_ipv4_127_subnet(proxy):
    """127.0.0.0/8 全段皆 loopback，非僅 127.0.0.1（禁字串比對的關鍵）。"""
    proxy(False)
    assert netutil.is_loopback(make_request(peer="127.1.2.3")) is True


def test_is_loopback_ipv6_peer(proxy):
    """peer=::1 → loopback True。"""
    proxy(False)
    assert netutil.is_loopback(make_request(peer="::1")) is True


def test_is_loopback_ipv4_mapped_ipv6(proxy):
    """::ffff:127.0.0.1（IPv4-mapped）須還原後判為 loopback，避免被當繞過漏洞。"""
    proxy(False)
    assert netutil.is_loopback(make_request(peer="::ffff:127.0.0.1")) is True


def test_is_loopback_public_peer_false(proxy):
    proxy(False)
    assert netutil.is_loopback(make_request(peer="203.0.113.9")) is False


def test_is_loopback_peer_none_false(proxy):
    """peer 不可知 → fail-closed False。"""
    proxy(False)
    assert netutil.is_loopback(make_request(peer=None)) is False


def test_is_loopback_malicious_leftmost_not_spoofable(proxy):
    """偽造防護：受信 peer + XFF 最左塞 127.0.0.1 → 取真實 client，is_loopback False。"""
    proxy(True)
    req = make_request(peer="127.0.0.1", xff="127.0.0.1, 203.0.113.9")
    assert netutil.is_loopback(req) is False


def test_is_loopback_real_client_loopback_via_proxy(proxy):
    """受信 peer + XFF 真實 client 為 ::1 → loopback True（正向走 XFF）。"""
    proxy(True)
    req = make_request(peer="127.0.0.1", xff="::1")
    assert netutil.is_loopback(req) is True


# --- QA 加嚴：fail-closed / 私網不誤判 / IPv4-mapped 經 XFF 偽造防護 ----
def test_is_loopback_garbage_peer_fail_closed(proxy):
    """peer 為無法解析的垃圾字串 → 包 try/except，fail-closed False（絕不誤判 loopback）。"""
    proxy(False)
    assert netutil.is_loopback(make_request(peer="not-an-ip")) is False


def test_is_loopback_private_non_loopback_false(proxy):
    """私網位址（10.0.0.1）非 loopback，務必回 False（與『內網』不可混為一談）。"""
    proxy(False)
    assert netutil.is_loopback(make_request(peer="10.0.0.1")) is False


def test_is_loopback_mapped_loopback_via_xff_not_spoofable(proxy):
    """受信 peer + XFF 最左偽造 ::ffff:127.0.0.1 → 取最右真實 client，is_loopback False。"""
    proxy(True, proxies="10.0.0.0/8")
    req = make_request(peer="10.0.0.1", xff="::ffff:127.0.0.1, 203.0.113.9")
    assert netutil.is_loopback(req) is False


def test_is_loopback_link_local_false(proxy):
    """link-local（fe80::1）非 loopback → False。"""
    proxy(False)
    assert netutil.is_loopback(make_request(peer="fe80::1")) is False
