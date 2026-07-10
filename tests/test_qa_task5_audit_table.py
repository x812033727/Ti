"""QA 獨立驗證：端點盤點表（docs/loopback-endpoint-audit.md）正確且完整。

把「文件」變成可執行的守門測試：自動從 app.routes 反查所有入口，與盤點表雙向比對。
- 完整性：每個實際 HTTP 入口都在盤點表中列載（無遺漏）。
- 正確性：盤點表標「✅ 納管」者，實際 deps 含 require_admin（管理門禁：門禁啟用→登入、
  停用→fail-safe 限本機）；未標者不含。
- 無虛構：盤點表列出的每個 (method, path) 都真實存在於 app。
- WS：/ws 已列載且不納管本機限定，ws.py 以 auth.is_authed 守護。
"""

from __future__ import annotations

import re

import pytest
from _repo import REPO_ROOT
from _routes import iter_routes
from fastapi.routing import APIRoute
from starlette.routing import WebSocketRoute

AUDIT = REPO_ROOT / "docs" / "loopback-endpoint-audit.md"
WS_SRC = REPO_ROOT / "studio" / "ws.py"
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
    """{(method, path): has_require_admin} for every APIRoute（自動排除框架/靜態）。"""
    out = {}
    for r in iter_routes(app):
        if isinstance(r, APIRoute):
            deps = {getattr(d.dependency, "__name__", None) for d in r.dependencies}
            for m in (r.methods or set()) - {"HEAD", "OPTIONS"}:
                out[(m, r.path)] = "require_admin" in deps
    return out


# --- 完整性：每個實際 HTTP 入口都被盤點 ----------------------------------
def test_audit_covers_every_http_route(app):
    http_docs, _ = parse_audit()
    actual = app_http_routes(app)
    missing = sorted(k for k in actual if k not in http_docs)
    assert not missing, f"盤點表遺漏入口：{missing}"


# --- 正確性：納管標記與實際 require_admin 完全一致 -------------------------
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


# --- 納管清單與架構決策一致（寫入端點全標 ✅）----------------------------
# 任務 #2 新增 /api/roles 寫入端點（POST/PUT/DELETE 走 WRITE_DEPS=require_admin），
# 依架構決策納管，與原六個寫入端點同列。
# #120 起 /api/groups 寫入端點（POST/PUT/DELETE）亦改用 WRITE_DEPS(require_admin)，
# 與 /api/roles 同級保護（groups.yaml 為組隊/mode 注入面），一併納管。
# Claude 多帳號：/api/claude-account/switch 走 WRITE_DEPS(require_admin)，換憑證檔並重啟
# 服務（高危狀態變更），依架構決策納管。
# #196 起 /api/publish/{session_id} 由 auth 升級為 WRITE_DEPS(require_admin)：對外發佈
# （push＋開 PR＋合併）屬對外狀態變更，與其他寫入端點同級納管。
# 動態流程：/api/workflows 寫入端點（POST/PUT/DELETE）走 WRITE_DEPS(require_admin)，
# 與 /api/groups 同級保護（workflows.yaml 為 stage 序列/角色/閘門注入面），一併納管。
# 派工模式：/api/autopilot/dispatch-mode 走 WRITE_DEPS(require_admin)，切哨兵檔改變後續
# session 的 provider/模型分配（auto＝PM 全權），與 pause/resume 同級納管。
def test_audit_managed_set_matches_decision():
    http_docs, _ = parse_audit()
    managed = {p for (m, p), mark in http_docs.items() if mark}
    expected = {
        "/api/redeploy",
        "/api/auth/password",
        "/api/settings",
        "/api/autopilot/pause",
        "/api/autopilot/resume",
        "/api/autopilot/dispatch-mode",
        "/api/autopilot/task",
        "/api/autopilot/triage",
        # 看板手動操作(C1):改寫 backlog 狀態(retry/park/unpark/priority),與 triage 同級納管。
        "/api/autopilot/task/{task_id}/action",
        "/api/roles",
        "/api/roles/{key}",
        "/api/groups",
        "/api/groups/{name}",
        "/api/workflows",
        "/api/workflows/{name}",
        "/api/claude-account/switch",
        "/api/publish/{session_id}",
    }
    assert managed == expected, f"納管清單與架構決策不符：{managed ^ expected}"


# --- WS：/ws 已列載但「不」納管本機限定，改以登入門禁守護 -----------------
def test_audit_ws_listed_and_auth_only(app):
    _, ws_docs = parse_audit()
    assert "/ws" in ws_docs, "盤點表未列載 /ws"
    assert ws_docs.get("/ws") is False, "盤點表不應把 /ws 標為（本機）納管：核心入口刻意不限本機"
    # app 確有 /ws WebSocket 入口
    ws_paths = {r.path for r in iter_routes(app) if isinstance(r, WebSocketRoute)}
    assert "/ws" in ws_paths
    # ws.py 改以 auth.is_authed 守護，且不再做本機限定
    src = WS_SRC.read_text(encoding="utf-8")
    assert "auth.is_authed(websocket)" in src
    assert "netutil.is_loopback(websocket)" not in src
