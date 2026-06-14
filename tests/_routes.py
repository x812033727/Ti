"""路由攤平輔助：相容新舊 starlette 的 `app.routes` 結構（測試共用）。

背景：starlette < 1.3 的 `app.include_router(sub)` 會把子路由「攤平」直接放進
`app.routes`，因此 `for r in app.routes` 能直接走到每個 `APIRoute`。starlette >= 1.3
改為在 `app.routes` 放一個延遲代理 `_IncludedRouter`（子路由收在
`proxy.original_router.routes`），不再攤平——以 `isinstance(r, APIRoute)` 或比對
`r.path`/`r.methods` 走訪 `app.routes` 的測試會在新版「找不到路由」而全紅。

`iter_routes()` 遞迴展開這層代理：遇到帶 `original_router` 的代理就進入其子 router，
其餘節點原樣產出。對舊版（無代理、已攤平）行為完全等價，故新舊版皆可用。
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any


def iter_routes(app: Any) -> Iterator[Any]:
    """產出 app（或 router）底下所有葉路由，遞迴展開 include_router 代理。"""
    yield from _iter(app, set())


def _iter(node: Any, seen: set[int]) -> Iterator[Any]:
    routes = getattr(node, "routes", None)
    if routes is None:
        return
    # 防環：同一 router 物件只展開一次。
    nid = id(node)
    if nid in seen:
        return
    seen.add(nid)
    for r in routes:
        orig = getattr(r, "original_router", None)
        if orig is not None:  # starlette >= 1.3 的 _IncludedRouter 代理
            yield from _iter(orig, seen)
        else:
            yield r
