"""POST /api/claude-account/switch 行為測試：409 busy 守衛、400 非法 label、200 成功重啟。

切換 Claude 在線帳號會重啟服務（中斷進行中討論／autopilot 任務），故 handler 先擋下
「進行中」狀態回 409，閒置才放行。此端點走 require_admin（門禁停用時退回 loopback），
測試以 loopback peer + ACCESS_PASSWORD="" 過門禁，並一律 monkeypatch 掉真正的服務重啟，
避免測試誤起 systemd-run／subprocess。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from studio import backlog, claude_accounts, config, routes, ws


@pytest.fixture
def client(monkeypatch):
    """門禁停用＋loopback peer（過 require_admin fail-safe）；預設閒置、重啟被擋。

    各測試可再覆寫 ws.active_session_count／backlog.list_tasks／claude_accounts.switch。
    fixture 一律把 _schedule_service_restart 換成 no-op，確保沒有任何測試能起真重啟。
    """
    monkeypatch.setattr(config, "ACCESS_PASSWORD", "")
    monkeypatch.setattr(ws, "active_session_count", lambda: 0)
    monkeypatch.setattr(backlog, "list_tasks", lambda status=None, **kw: [])
    monkeypatch.setattr(routes, "_schedule_service_restart", lambda: None)
    from studio.server import app

    return TestClient(app, client=("127.0.0.1", 12345))


def test_switch_busy_active_discussion_409(client, monkeypatch):
    monkeypatch.setattr(ws, "active_session_count", lambda: 1)
    res = client.post("/api/claude-account/switch", json={"label": "work"})
    assert res.status_code == 409
    body = res.json()
    assert body["ok"] is False and body["error"] == "busy"
    assert any("互動討論" in r for r in body["reasons"])


def test_switch_busy_autopilot_tasks_409(client, monkeypatch):
    monkeypatch.setattr(backlog, "list_tasks", lambda status=None, **kw: [{"id": 1}, {"id": 2}])
    res = client.post("/api/claude-account/switch", json={"label": "work"})
    assert res.status_code == 409
    body = res.json()
    assert body["error"] == "busy"
    assert any("2 個任務" in r for r in body["reasons"])


def test_switch_invalid_label_400_no_restart(client, monkeypatch):
    def _raise(label):
        raise ValueError("未知帳號標籤")

    monkeypatch.setattr(claude_accounts, "switch", _raise)
    restarts: list[bool] = []
    monkeypatch.setattr(routes, "_schedule_service_restart", lambda: restarts.append(True))

    res = client.post("/api/claude-account/switch", json={"label": "ghost"})
    assert res.status_code == 400
    body = res.json()
    assert body["ok"] is False
    assert "未知帳號標籤" in body["error"]
    assert restarts == []  # 切換失敗不得觸發重啟


def test_switch_success_200_and_schedules_restart(client, monkeypatch):
    switched: list[str] = []
    monkeypatch.setattr(claude_accounts, "switch", lambda label: switched.append(label))
    restarts: list[bool] = []
    monkeypatch.setattr(routes, "_schedule_service_restart", lambda: restarts.append(True))

    res = client.post("/api/claude-account/switch", json={"label": "work"})
    assert res.status_code == 200
    assert res.json() == {"ok": True, "label": "work", "restarting": True}
    assert switched == ["work"]  # 切換以該 label 呼叫一次
    assert restarts == [True]  # 重啟被排程一次
