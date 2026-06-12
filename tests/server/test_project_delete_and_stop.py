"""刪除專案與停止執行 API：檔案清除、409 守衛、停止管線接線與 404。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from studio import config as cfg, projects, ws


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "ACCESS_PASSWORD", "")
    monkeypatch.setattr(cfg, "PROJECTS_ROOT", tmp_path / "projects")
    monkeypatch.setattr(cfg, "WORKSPACE_ROOT", tmp_path / "ws")
    from studio.server import app

    return TestClient(app)


# --- DELETE /api/projects/{pid} -----------------------------------------


def test_delete_project_removes_meta_workspace_and_lanes(client):
    pid = projects.create("要刪的產品")["id"]
    ws_dir = projects.workspace_dir(pid)
    (ws_dir / "main.py").write_text("print('hi')\n", encoding="utf-8")
    lanes = ws_dir.parent / f"{ws_dir.name}.lanes"
    lanes.mkdir()
    (lanes / "leftover").write_text("殘留", encoding="utf-8")

    res = client.delete(f"/api/projects/{pid}")
    assert res.status_code == 200 and res.json()["ok"] is True
    assert projects.get(pid) is None
    assert not projects.state_dir(pid).exists()
    assert not ws_dir.exists()
    assert not lanes.exists()
    # 列表與 detail 同步消失
    assert pid not in [p["id"] for p in client.get("/api/projects").json()["projects"]]
    assert client.get(f"/api/projects/{pid}").status_code == 404


def test_delete_unknown_project_404(client):
    assert client.delete("/api/projects/nope").status_code == 404


def test_delete_active_project_409_and_keeps_files(client, monkeypatch):
    """進行中的專案拒刪（409），檔案一個都不能少。"""
    pid = projects.create("進行中")["id"]
    ws_dir = projects.workspace_dir(pid)
    monkeypatch.setattr(ws, "_active_projects", {pid})
    res = client.delete(f"/api/projects/{pid}")
    assert res.status_code == 409
    assert projects.get(pid) is not None
    assert ws_dir.exists()


def test_project_detail_reports_active_flag(client, monkeypatch):
    pid = projects.create("旗標")["id"]
    assert client.get(f"/api/projects/{pid}").json()["active"] is False
    monkeypatch.setattr(ws, "_active_projects", {pid})
    assert client.get(f"/api/projects/{pid}").json()["active"] is True


def test_projects_delete_function_returns_false_for_unknown():
    assert projects.delete("missing") is False


# --- POST /api/sessions/{target_id}/stop ----------------------------------


class _FakeController:
    """request_stop 介面與 StudioSession/ProjectImprover 對齊的測試替身。"""

    def __init__(self, record_sid: str | None = None):
        self.stopped = False
        self._record_sid = record_sid

    def request_stop(self) -> None:
        self.stopped = True


@pytest.fixture
def registry(monkeypatch):
    reg: dict[str, object] = {}
    monkeypatch.setattr(ws, "_running", reg)
    return reg


def test_stop_by_session_id(client, registry):
    ctl = _FakeController()
    registry["abc123"] = ctl
    res = client.post("/api/sessions/abc123/stop")
    assert res.status_code == 200 and res.json()["ok"] is True
    assert ctl.stopped is True


def test_stop_improver_by_inner_round_sid(client, registry):
    """改良迴圈：歷史列表顯示的是該輪的 session id（_record_sid），也要停得掉。"""
    ctl = _FakeController(record_sid="pj0123456789")
    registry["improve-pid1"] = ctl
    res = client.post("/api/sessions/pj0123456789/stop")
    assert res.status_code == 200
    assert ctl.stopped is True


def test_stop_unknown_target_404(client, registry):
    assert client.post("/api/sessions/ghost/stop").status_code == 404


def test_register_unregister_running(registry):
    ctl = _FakeController()
    ws._register_running(ctl, "sid", "pid")
    assert ws.stop_running("pid") is True
    ws._unregister_running("sid", "pid")
    assert ws.stop_running("sid") is False and ws.stop_running("pid") is False
