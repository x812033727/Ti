"""QA 獨立驗證：任務 #5 端點盤點表（docs/loopback-endpoint-audit.md）正確且完整。

把「文件」變成可執行的守門測試：自動從 app.routes 反查所有入口，與盤點表雙向比對。
- 完整性：每個實際 HTTP 入口都在盤點表中列載（無遺漏）。
- 正確性：盤點表標「✅ 納管」者，實際 deps 含 require_loopback；未標者不含。
- 無虛構：盤點表列出的每個 (method, path) 都真實存在於 app。
- WS：/ws 已列載且標納管，且 ws.py 確實於 handler 內以 netutil.is_loopback 檢查。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from starlette.routing import WebSocketRoute

AUDIT = Path(__file__).resolve().parent.parent / "docs" / "loopback-endpoint-audit.md"
WS_SRC = Path(__file__).resolve().parent.parent / "studio" / "ws.py"
HTTP_METHODS = {"GET", "POST", "DELETE", "PUT", "PATCH"}


@pytest.fixture(scope="module")
def app():
    from studio.server import app as fastapi_app

    return fastapi_app


def parse_audit():
    """解析盤點表，回傳 (http_docs, ws_docs)。

    http_docs: {(method, path): has_loopback_mark}
    ws_docs:   {path: has_loopback_mark}
    """
    http_docs: dict[tuple[str, str], bool] = {}
    ws_docs: dict[str, bool] = {}
    for line in AUDIT.read_text(encoding="utf-8").splitlines():
        if not line.lstrip().startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if not cells or set(cells[0]) <= set("-: "):  # 分隔列
            continue
        method = cells[0].strip("` ").upper()
        # 抽出整列所有反引號 token 中以 / 開頭者當路徑
        paths = [t for t in re.findall(r"`([^`]+)`", line) if t.startswith("/")]
        if not paths:
            continue
        has_mark = "✅" in line
        if method in HTTP_METHODS:
            for p in paths:
                http_docs[(method, p)] = has_mark
        elif "/ws" in paths:  # WS 表列（第一欄非 HTTP method）
            ws_docs["/ws"] = has_mark
    return http_docs, ws_docs


def app_http_routes(app):
    """{(method, path): has_require_loopback} for every APIRoute（自動排除框架/靜態）。"""
    out = {}
    for r in app.routes:
        if isinstance(r, APIRoute):
            deps = {getattr(d.dependency, "__name__", None) for d in r.dependencies}
            for m in (r.methods or set()) - {"HEAD", "OPTIONS"}:
                out[(m, r.path)] = "require_loopback" in deps
    return out


# --- 完整性：每個實際 HTTP 入口都被盤點 ----------------------------------
def test_audit_covers_every_http_route(app):
    http_docs, _ = parse_audit()
    actual = app_http_routes(app)
    missing = sorted(k for k in actual if k not in http_docs)
    assert not missing, f"盤點表遺漏入口：{missing}"


# --- 正確性：納管標記與實際 require_loopback 完全一致 ---------------------
def test_audit_marks_match_actual_deps(app):
    http_docs, _ = parse_audit()
    actual = app_http_routes(app)
    mismatch = {
        k: (http_docs[k], actual[k]) for k in actual if k in http_docs and http_docs[k] != actual[k]
    }
    assert not mismatch, f"盤點標記與實際 deps 不符 (doc, actual)：{mismatch}"


# --- 無虛構：盤點表列出的 HTTP 路徑都真實存在 ----------------------------
def test_audit_has_no_phantom_routes(app):
    http_docs, _ = parse_audit()
    actual = app_http_routes(app)
    phantom = sorted(k for k in http_docs if k not in actual)
    assert not phantom, f"盤點表列出不存在的路由：{phantom}"


# --- 納管清單與架構決策一致（六個寫入端點全標 ✅）------------------------
def test_audit_managed_set_matches_decision():
    http_docs, _ = parse_audit()
    managed = {p for (m, p), mark in http_docs.items() if mark}
    expected = {
        "/api/redeploy",
        "/api/auth/password",
        "/api/settings",
        "/api/autopilot/pause",
        "/api/autopilot/resume",
        "/api/autopilot/task",
    }
    assert managed == expected, f"納管清單與架構決策不符：{managed ^ expected}"


# --- WS：/ws 已列載並標納管，且 handler 內確實檢查 -----------------------
def test_audit_ws_listed_and_managed(app):
    _, ws_docs = parse_audit()
    assert ws_docs.get("/ws") is True, "盤點表未把 /ws 標為納管"
    # app 確有 /ws WebSocket 入口
    ws_paths = {r.path for r in app.routes if isinstance(r, WebSocketRoute)}
    assert "/ws" in ws_paths
    # ws.py 確實於 handler 內呼叫 netutil.is_loopback（非靠路由依賴）
    src = WS_SRC.read_text(encoding="utf-8")
    assert "netutil.is_loopback(websocket)" in src
