from __future__ import annotations

import _loopback_clients


def test_loopback_websocket_connect_disables_proxy_when_supported(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_connect(uri: str, **kwargs):
        captured["uri"] = uri
        captured["kwargs"] = kwargs
        return "conn"

    monkeypatch.setattr(_loopback_clients, "_CONNECT", fake_connect)
    monkeypatch.setattr(_loopback_clients, "_CONNECT_SUPPORTS_PROXY", True)

    assert _loopback_clients.loopback_websocket_connect("ws://x", max_size=1) == "conn"
    assert captured == {"uri": "ws://x", "kwargs": {"max_size": 1, "proxy": None}}


def test_loopback_websocket_connect_omits_proxy_when_unsupported(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_connect(uri: str, **kwargs):
        captured["uri"] = uri
        captured["kwargs"] = kwargs
        return "conn"

    monkeypatch.setattr(_loopback_clients, "_CONNECT", fake_connect)
    monkeypatch.setattr(_loopback_clients, "_CONNECT_SUPPORTS_PROXY", False)

    assert _loopback_clients.loopback_websocket_connect("ws://x", max_size=1) == "conn"
    assert captured == {"uri": "ws://x", "kwargs": {"max_size": 1}}
