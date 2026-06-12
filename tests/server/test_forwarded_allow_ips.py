"""issue #0001：uvicorn ProxyHeaders 信任鏈落地測試。

對應驗收標準：
- forwarded_allow_ips 由 env 提供、預設安全值（本機），嚴禁硬編 "*"；偵測到 "*" 拒啟動。
- server.main() 明確傳入 proxy_headers=True 與 forwarded_allow_ips=<可信來源>。
- pyproject 下限已升至 >=0.31（CIDR/IPv6 信任網段保證）。

沿用 tests/server/test_trust_proxy.py 的 reload_config fixture 模式（setenv → reload(config)）。
"""

from __future__ import annotations

import importlib
import re
import sys
import types

import pytest
from _repo import REPO_ROOT

from studio import config

try:
    import tomllib  # py3.11+
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib


@pytest.fixture
def reload_config(monkeypatch):
    """以指定環境變數 reload config，teardown 還原為預設。"""

    def _set(**env):
        for k, v in env.items():
            if v is None:
                monkeypatch.delenv(k, raising=False)
            else:
                monkeypatch.setenv(k, v)
        importlib.reload(config)
        return config

    yield _set
    for k in ("TI_FORWARDED_ALLOW_IPS", "FORWARDED_ALLOW_IPS"):
        monkeypatch.delenv(k, raising=False)
    importlib.reload(config)


# --- 驗收標準 2：預設安全值（本機）------------------------------------
def test_default_is_loopback(reload_config):
    cfg = reload_config(TI_FORWARDED_ALLOW_IPS=None, FORWARDED_ALLOW_IPS=None)
    assert cfg.forwarded_allow_ips() == "127.0.0.1"


def test_empty_falls_back_to_loopback(reload_config):
    """空字串（誤設空值）退回安全預設，不變成 uvicorn 的 '' 全擋或預設。"""
    cfg = reload_config(TI_FORWARDED_ALLOW_IPS="")
    assert cfg.forwarded_allow_ips() == "127.0.0.1"


# --- env 覆寫與無前綴別名 ----------------------------------------------
def test_env_overrides(reload_config):
    cfg = reload_config(TI_FORWARDED_ALLOW_IPS="10.0.0.0/8,172.16.0.0/12")
    assert cfg.forwarded_allow_ips() == "10.0.0.0/8,172.16.0.0/12"


def test_unprefixed_alias(reload_config):
    """未設 TI_ 前綴時，沿用 uvicorn 慣用名 FORWARDED_ALLOW_IPS。"""
    cfg = reload_config(TI_FORWARDED_ALLOW_IPS=None, FORWARDED_ALLOW_IPS="192.168.0.0/16")
    assert cfg.forwarded_allow_ips() == "192.168.0.0/16"


def test_prefixed_takes_precedence(reload_config):
    cfg = reload_config(TI_FORWARDED_ALLOW_IPS="10.0.0.1", FORWARDED_ALLOW_IPS="192.168.0.1")
    assert cfg.forwarded_allow_ips() == "10.0.0.1"


# --- 驗收標準 2：嚴禁 "*"（fail-closed 拒啟動）------------------------
def test_wildcard_refused(reload_config):
    cfg = reload_config(TI_FORWARDED_ALLOW_IPS="*")
    with pytest.raises(SystemExit):
        cfg.forwarded_allow_ips()


def test_wildcard_among_others_refused(reload_config):
    """清單中夾帶 "*"（如 "10.0.0.0/8,*"）一樣拒啟動，不被其它合法項掩護。"""
    cfg = reload_config(TI_FORWARDED_ALLOW_IPS="10.0.0.0/8, *")
    with pytest.raises(SystemExit):
        cfg.forwarded_allow_ips()


# --- 驗收標準 1/5：main() 確實把 proxy kwargs 傳給 uvicorn.run ----------
def _fake_uvicorn(monkeypatch):
    """注入假 uvicorn 模組，截下 run() 的 kwargs；回傳 captured dict。"""
    captured: dict = {}

    def _run(app, **kwargs):
        captured.update(kwargs)

    fake = types.ModuleType("uvicorn")
    fake.run = _run
    monkeypatch.setitem(sys.modules, "uvicorn", fake)
    return captured


def test_main_passes_proxy_kwargs(reload_config, monkeypatch):
    cfg = reload_config(TI_FORWARDED_ALLOW_IPS="10.0.0.0/8")
    captured = _fake_uvicorn(monkeypatch)
    from studio import server

    server.main()
    assert captured.get("proxy_headers") is True
    assert captured.get("forwarded_allow_ips") == "10.0.0.0/8"
    assert captured.get("forwarded_allow_ips") != "*"
    assert captured["forwarded_allow_ips"] == cfg.forwarded_allow_ips()


def test_main_refuses_wildcard_before_run(reload_config, monkeypatch):
    """env 設 '*' → main() 在呼叫 uvicorn.run 之前就 SystemExit。"""
    reload_config(TI_FORWARDED_ALLOW_IPS="*")
    captured = _fake_uvicorn(monkeypatch)
    from studio import server

    with pytest.raises(SystemExit):
        server.main()
    assert captured == {}  # run 未被呼叫


# --- 驗收標準 4：pyproject 下限已升至 >=0.31 ---------------------------
def test_pyproject_lower_bound_031():
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    deps = data["project"]["dependencies"]
    dep = next(d for d in deps if d.lower().lstrip().startswith("uvicorn"))
    m = re.search(r">=\s*([0-9]+)\.([0-9]+)", dep)
    assert m, f"uvicorn 依賴須有 >= 下限：{dep}"
    major, minor = int(m.group(1)), int(m.group(2))
    assert (major, minor) >= (0, 31), f"下限需 >=0.31（CIDR/IPv6 信任網段）：{dep}"
