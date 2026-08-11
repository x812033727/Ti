"""History 破壞性端點測試：清理/刪除行為與未設密碼時的本機限制。

cleanup/completed 委派 history.delete_completed_sessions（破壞性）。retention 端點已由
tests/core/test_history_retention.py 覆蓋，本檔專補 completed 這條原本零覆蓋的路徑，
以及三個 history 破壞性端點在門禁停用時只允許 loopback peer。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from studio import config, history


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ACCESS_PASSWORD", "")  # require_admin 放行 loopback
    monkeypatch.setattr(config, "HISTORY_ROOT", tmp_path / "history")
    monkeypatch.setattr(config, "WORKSPACE_ROOT", tmp_path / "workspaces")
    from studio.server import app

    return TestClient(app, client=("127.0.0.1", 12345))


@pytest.fixture
def public_client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ACCESS_PASSWORD", "")
    monkeypatch.setattr(config, "HISTORY_ROOT", tmp_path / "history")
    monkeypatch.setattr(config, "WORKSPACE_ROOT", tmp_path / "workspaces")
    from studio.server import app

    return TestClient(app, client=("203.0.113.5", 40000))


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


def test_public_peer_cannot_cleanup_completed(public_client):
    _make("done1", "completed")
    _make("running1", "running")
    res = public_client.post("/api/history/cleanup/completed")
    assert res.status_code == 403
    assert _ids() == {"done1", "running1"}


def test_public_peer_cannot_cleanup_retention(public_client, monkeypatch):
    monkeypatch.setattr(config, "HISTORY_MAX_COUNT", 1)
    monkeypatch.setattr(config, "HISTORY_MAX_AGE", 0)
    _make("old1", "completed")
    _make("old2", "completed")
    res = public_client.post("/api/history/cleanup/retention")
    assert res.status_code == 403
    assert _ids() == {"old1", "old2"}


def test_public_peer_cannot_delete_session(public_client):
    _make("done1", "completed")
    res = public_client.delete("/api/history/done1")
    assert res.status_code == 403
    assert _ids() == {"done1"}
