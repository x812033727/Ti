"""Loopback client helpers shared by real-server smoke tests."""

from __future__ import annotations

import importlib
import inspect
from typing import Any

_WEBSOCKETS = importlib.import_module("websockets")
_CONNECT = _WEBSOCKETS.connect


def _connect_supports_proxy_kwarg() -> bool:
    try:
        return "proxy" in inspect.signature(_CONNECT).parameters
    except (TypeError, ValueError):
        return False


_CONNECT_SUPPORTS_PROXY = _connect_supports_proxy_kwarg()


def loopback_websocket_connect(uri: str, **kwargs: Any) -> Any:
    """Disable host proxy env on websockets versions that support the proxy kwarg."""
    if _CONNECT_SUPPORTS_PROXY:
        kwargs.setdefault("proxy", None)
    return _CONNECT(uri, **kwargs)
