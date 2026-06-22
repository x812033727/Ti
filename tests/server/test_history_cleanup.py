"""POST /api/history/cleanup/completed 行為測試：只刪已完成場、保留其餘、空集合回 0。

cleanup/completed 委派 history.delete_completed_sessions（破壞性）。retention 端點已由
tests/core/test_history_retention.py 覆蓋，本檔專補 completed 這條原本零覆蓋的路徑。
端點走 require_auth；門禁停用（ACCESS_PASSWORD=""）即放行，無需 loopback peer。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from studio import config, history


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ACCESS_PASSWORD", "")  # require_auth 放行
    monkeypatch.setattr(config, "HISTORY_ROOT", tmp_path / "history")
    monkeypatch.setattr(config, "WORKSPACE_ROOT", tmp_path / "workspaces")
    from studio.server import app

    return TestClient(app)


def _make(sid: str, status: str) -> None:
    meta = history.start_session(sid, f"req-{sid}")
    meta["status"] = status
    history._write_meta(sid, meta)


def _ids() -> set[str]:
    return {m["session_id"] for m in history.list_sessions()}


def test_cleanup_completed_removes_only_completed(client):
    _make("done1", "completed")
    _make("done2", "completed")
    _make("running1", "running")  # 非 completed 一律保留
    res = client.post("/api/history/cleanup/completed")
    assert res.status_code == 200
    assert res.json() == {"deleted": 2}
    assert _ids() == {"running1"}


def test_cleanup_completed_empty_returns_zero(client):
    _make("running1", "running")
    res = client.post("/api/history/cleanup/completed")
    assert res.status_code == 200
    assert res.json() == {"deleted": 0}
    assert _ids() == {"running1"}  # 無可清場次，原樣留存
