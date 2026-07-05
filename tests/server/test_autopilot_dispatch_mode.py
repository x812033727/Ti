"""POST /api/autopilot/dispatch-mode 派工模式哨兵檔切換 + GET /api/autopilot 回傳現值。

契約：auto → 寫 config.DISPATCH_AUTO_FILE；manual → 刪檔；非法值 400；GET 的
dispatch_mode 永遠反映哨兵檔現況（預設 manual）。寫入端點掛 WRITE_DEPS（門禁停用時
僅限 loopback，比照 pause/resume）。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from studio import config

LOOPBACK_PEER = ("127.0.0.1", 12345)
PUBLIC_PEER = ("203.0.113.7", 12345)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ACCESS_PASSWORD", "")  # 門禁停用 → 寫入端點退回僅限 loopback
    monkeypatch.setattr(config, "AUTOPILOT_STATE_DIR", tmp_path)
    monkeypatch.setattr(config, "AUTOPILOT_PAUSE_FILE", tmp_path / "AUTOPILOT_PAUSED")
    monkeypatch.setattr(config, "DISPATCH_AUTO_FILE", tmp_path / "DISPATCH_AUTO")
    from studio.server import app

    return TestClient(app, client=LOOPBACK_PEER)


def test_dispatch_mode_defaults_to_manual(client):
    data = client.get("/api/autopilot").json()
    assert data["dispatch_mode"] == "manual"


def test_switch_to_auto_writes_sentinel_and_reflects(client):
    res = client.post("/api/autopilot/dispatch-mode", json={"mode": "auto"})
    assert res.status_code == 200 and res.json()["dispatch_mode"] == "auto"
    assert config.DISPATCH_AUTO_FILE.exists()
    assert client.get("/api/autopilot").json()["dispatch_mode"] == "auto"


def test_switch_back_to_manual_removes_sentinel(client):
    client.post("/api/autopilot/dispatch-mode", json={"mode": "auto"})
    res = client.post("/api/autopilot/dispatch-mode", json={"mode": "manual"})
    assert res.status_code == 200 and res.json()["dispatch_mode"] == "manual"
    assert not config.DISPATCH_AUTO_FILE.exists()


def test_invalid_mode_rejected(client):
    res = client.post("/api/autopilot/dispatch-mode", json={"mode": "yolo"})
    assert res.status_code == 400
    assert not config.DISPATCH_AUTO_FILE.exists()
    # 空值也拒絕
    assert client.post("/api/autopilot/dispatch-mode", json={}).status_code == 400


def test_write_gate_blocks_non_loopback(tmp_path, monkeypatch):
    """門禁停用時寫入端點僅限本機——公網來源 403（與 pause/resume 同一道 WRITE_DEPS）。"""
    monkeypatch.setattr(config, "ACCESS_PASSWORD", "")
    monkeypatch.setattr(config, "DISPATCH_AUTO_FILE", tmp_path / "DISPATCH_AUTO")
    from studio.server import app

    public = TestClient(app, client=PUBLIC_PEER)
    res = public.post("/api/autopilot/dispatch-mode", json={"mode": "auto"})
    assert res.status_code == 403
    assert not config.DISPATCH_AUTO_FILE.exists()
