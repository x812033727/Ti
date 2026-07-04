"""improve（持續改良迴圈）模式的 WS 斷線重掛守護。

契約：improve umbrella 沒有單一 JSONL（各輪各自入檔），故 attach 走 live-only——
不補放歷史、attach_ok 帶 live_only=true、cursor=0，只接續即時事件；attach 端
interject 仍回拋到 improver 的 intervention queue；可用 umbrella id 或當前輪的
_record_sid 掛上（同 stop_running 的 fallback 慣例）。
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from studio import config, events, projects, ws
from studio.improver import ProjectImprover


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ACCESS_PASSWORD", "")
    monkeypatch.setattr(config, "OFFLINE_MODE", True)
    monkeypatch.setattr(config, "MAX_CONCURRENT_SESSIONS", 4)
    monkeypatch.setattr(config, "PROJECTS_ROOT", tmp_path / "projects")
    monkeypatch.setattr(config, "WORKSPACE_ROOT", tmp_path / "ws")
    monkeypatch.setattr(config, "HISTORY_ROOT", tmp_path / "hist")
    monkeypatch.setattr(ws, "_active_sessions", 0)
    monkeypatch.setattr(ws, "_active_projects", set())
    ws._hubs.clear()
    yield
    ws._hubs.clear()


def _install_improve_stub(monkeypatch, *, set_record_sid: str | None = None):
    """把 ProjectImprover.run 換成可控 stub：broadcast 一筆 → 等 interject → broadcast 收尾。"""

    async def _stub(self, max_cycles=None):
        if set_record_sid is not None:
            self._record_sid = set_record_sid  # 模擬「當前輪」的 record sid
        await self.broadcast(events.phase_change(self.session_id, "改良中", "第一輪"))
        text = await asyncio.wait_for(self.queue.get(), timeout=20)
        await self.broadcast(events.phase_change(self.session_id, "續作", text))
        await self.broadcast(
            events.StudioEvent(
                events.EventType.DONE,
                self.session_id,
                {"completed": True, "improve": {"cycles": 1, "done": 1, "failed": 0}},
            )
        )
        return {"cycles": 1, "done": 1, "failed": 0, "stopped": False}

    monkeypatch.setattr(ProjectImprover, "run", _stub)


def _client():
    from studio.server import app

    return TestClient(app, client=("127.0.0.1", 12345))


def _recv_until(conn, type_, limit=200):
    got = []
    for _ in range(limit):
        ev = conn.receive_json()
        got.append(ev)
        if ev["type"] == type_:
            return ev, got
    raise AssertionError(f"未在 {limit} 筆內收到 {type_}：{[e['type'] for e in got]}")


def test_improve_attach_is_live_only(monkeypatch):
    _install_improve_stub(monkeypatch)
    project = projects.create("測試產品", vision="願景")
    pid = project["id"]
    umbrella = f"improve-{pid}"
    client = _client()
    with client.websocket_connect("/ws") as main:
        main.send_json({"project_id": pid, "mode": "improve"})
        # 主連線收到第一筆（stub 停在等 interject）
        _recv_until(main, "phase_change")

        with client.websocket_connect("/ws") as att:
            att.send_json({"attach": umbrella, "cursor": 0})
            ok = att.receive_json()
            assert ok["type"] == "attach_ok"
            assert ok["payload"]["live_only"] is True  # improve＝live-only
            assert ok["payload"]["cursor"] == 0  # 無補放
            # 解除 stub → live 事件雙端都收到
            main.send_json({"type": "interject", "text": "換個方向"})
            ev, got = _recv_until(att, "done")
            phases = [e["payload"].get("phase") for e in got if e["type"] == "phase_change"]
            assert "續作" in phases  # attach 端接到 interject 後的即時事件
        _recv_until(main, "done")


def test_improve_attach_via_round_record_sid(monkeypatch):
    """可用當前輪的 _record_sid 掛上（前端記到的是輪 sid 而非 umbrella id）。"""
    _install_improve_stub(monkeypatch, set_record_sid="round-abc")
    project = projects.create("測試產品", vision="願景")
    pid = project["id"]
    client = _client()
    with client.websocket_connect("/ws") as main:
        main.send_json({"project_id": pid, "mode": "improve"})
        _recv_until(main, "phase_change")

        with client.websocket_connect("/ws") as att:
            att.send_json({"attach": "round-abc", "cursor": 0})  # 輪 sid，非 umbrella
            ok = att.receive_json()
            assert ok["type"] == "attach_ok" and ok["payload"]["live_only"] is True
            main.send_json({"type": "interject", "text": "go"})
            _recv_until(att, "done")
        _recv_until(main, "done")


def test_improve_attach_interject_reaches_queue(monkeypatch):
    _install_improve_stub(monkeypatch)
    project = projects.create("測試產品", vision="願景")
    pid = project["id"]
    umbrella = f"improve-{pid}"
    client = _client()
    with client.websocket_connect("/ws") as main:
        main.send_json({"project_id": pid, "mode": "improve"})
        _recv_until(main, "phase_change")
        with client.websocket_connect("/ws") as att:
            att.send_json({"attach": umbrella, "cursor": 0})
            _recv_until(att, "attach_ok")
            # 從 attach 端插話 → 進 improver.queue → stub 收到後續作，續作 phase 帶回文字
            att.send_json({"type": "interject", "text": "從重掛端插話"})
            ev, got = _recv_until(att, "done")
            detail = next(
                (e["payload"].get("detail") for e in got if e["payload"].get("phase") == "續作"),
                "",
            )
            assert detail == "從重掛端插話"
        _recv_until(main, "done")
