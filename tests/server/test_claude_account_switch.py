"""POST /api/claude-account/switch 行為測試：409 busy 守衛、強制（force）、排隊
（queue→pin）、400 非法 label、200 成功重啟＋釘選；DELETE /api/claude-account/pin 解除釘選。

切換 Claude 在線帳號會重啟服務（中斷進行中討論／autopilot 任務），故 handler 預設擋下
「進行中」狀態：force=True（UI 忙碌路徑）跳過守衛立即切＋重啟（回應標 forced，優雅停機
把被中斷任務退回 pending）；queue=True（API 選項）寫 pin 檔回 202 由 autopilot 任務空檔
代切；兩者皆無回 409（附 queueable）。成功切換（立即/強制/排隊）都會釘選＝凍結自動輪替。
此端點走 require_admin（門禁停用時退回 loopback），測試以 loopback peer +
ACCESS_PASSWORD="" 過門禁，並一律 monkeypatch 掉真正的服務重啟，避免測試誤起
systemd-run／subprocess。
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
    assert body["queueable"] is True  # 前端據此提示「可排隊」
    assert any("互動討論" in r for r in body["reasons"])


def test_switch_busy_autopilot_tasks_409(client, monkeypatch):
    monkeypatch.setattr(backlog, "list_tasks", lambda status=None, **kw: [{"id": 1}, {"id": 2}])
    res = client.post("/api/claude-account/switch", json={"label": "work"})
    assert res.status_code == 409
    body = res.json()
    assert body["error"] == "busy"
    assert body["queueable"] is True
    assert any("2 個任務" in r for r in body["reasons"])


def test_switch_busy_queue_202_pins_without_switch(client, monkeypatch):
    """忙碌＋queue=True → 202 排隊：只寫 pin，不切換、不重啟（代切交給 autopilot）。"""
    monkeypatch.setattr(backlog, "list_tasks", lambda status=None, **kw: [{"id": 1}])
    monkeypatch.setattr(claude_accounts, "label_exists", lambda label: True)
    pinned: list[str | None] = []
    monkeypatch.setattr(claude_accounts, "set_pinned", lambda label: pinned.append(label))
    switched: list[str] = []
    monkeypatch.setattr(claude_accounts, "switch", lambda label: switched.append(label))
    restarts: list[bool] = []
    monkeypatch.setattr(routes, "_schedule_service_restart", lambda: restarts.append(True))

    res = client.post("/api/claude-account/switch", json={"label": "work", "queue": True})
    assert res.status_code == 202
    body = res.json()
    assert body["ok"] is True and body["queued"] is True and body["label"] == "work"
    assert body["reasons"]  # 告知前端排隊原因
    assert pinned == ["work"]
    assert switched == [] and restarts == []  # 不立即切換、不重啟


def test_switch_busy_force_200_switches_immediately(client, monkeypatch):
    """忙碌＋force=True（UI 忙碌路徑）→ 跳過守衛立即切＋釘選＋重啟，回應標 forced。"""
    monkeypatch.setattr(backlog, "list_tasks", lambda status=None, **kw: [{"id": 1}])
    calls: list[tuple[str, str | None]] = []
    monkeypatch.setattr(claude_accounts, "switch", lambda label: calls.append(("switch", label)))
    monkeypatch.setattr(claude_accounts, "set_pinned", lambda label: calls.append(("pin", label)))
    restarts: list[bool] = []
    monkeypatch.setattr(routes, "_schedule_service_restart", lambda: restarts.append(True))

    res = client.post("/api/claude-account/switch", json={"label": "work", "force": True})
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True and body["restarting"] is True and body["pinned"] is True
    assert body["forced"] is True  # 中斷式切換的標記（閒置切換無此欄位）
    assert calls == [("switch", "work"), ("pin", "work")]
    assert restarts == [True]


def test_switch_busy_force_invalid_label_400_no_pin_no_restart(client, monkeypatch):
    """忙碌＋force 但 switch 拋 ValueError → 400，不釘選、不重啟。"""
    monkeypatch.setattr(ws, "active_session_count", lambda: 1)

    def _raise(label):
        raise ValueError("未知帳號標籤")

    monkeypatch.setattr(claude_accounts, "switch", _raise)
    pinned: list[str | None] = []
    monkeypatch.setattr(claude_accounts, "set_pinned", lambda label: pinned.append(label))
    restarts: list[bool] = []
    monkeypatch.setattr(routes, "_schedule_service_restart", lambda: restarts.append(True))

    res = client.post("/api/claude-account/switch", json={"label": "ghost", "force": True})
    assert res.status_code == 400
    assert pinned == [] and restarts == []


def test_switch_idle_success_has_no_forced_flag(client, monkeypatch):
    """閒置切換不標 forced（沒有中斷任何東西）。"""
    monkeypatch.setattr(claude_accounts, "switch", lambda label: None)
    monkeypatch.setattr(claude_accounts, "set_pinned", lambda label: None)

    res = client.post("/api/claude-account/switch", json={"label": "work", "force": True})
    assert res.status_code == 200
    assert "forced" not in res.json()


def test_switch_busy_queue_unknown_label_400_no_pin(client, monkeypatch):
    """忙碌＋queue=True 但目標憑證檔不存在 → 400，pin 不得寫入（防壞 pin 進系統）。"""
    monkeypatch.setattr(backlog, "list_tasks", lambda status=None, **kw: [{"id": 1}])
    monkeypatch.setattr(claude_accounts, "label_exists", lambda label: False)
    pinned: list[str | None] = []
    monkeypatch.setattr(claude_accounts, "set_pinned", lambda label: pinned.append(label))

    res = client.post("/api/claude-account/switch", json={"label": "ghost", "queue": True})
    assert res.status_code == 400
    assert res.json()["ok"] is False
    assert pinned == []


def test_switch_invalid_label_400_no_restart_no_pin(client, monkeypatch):
    def _raise(label):
        raise ValueError("未知帳號標籤")

    monkeypatch.setattr(claude_accounts, "switch", _raise)
    pinned: list[str | None] = []
    monkeypatch.setattr(claude_accounts, "set_pinned", lambda label: pinned.append(label))
    restarts: list[bool] = []
    monkeypatch.setattr(routes, "_schedule_service_restart", lambda: restarts.append(True))

    res = client.post("/api/claude-account/switch", json={"label": "ghost"})
    assert res.status_code == 400
    body = res.json()
    assert body["ok"] is False
    assert "未知帳號標籤" in body["error"]
    assert restarts == []  # 切換失敗不得觸發重啟
    assert pinned == []  # 切換失敗不得寫 pin


def test_switch_success_200_schedules_restart_and_pins(client, monkeypatch):
    calls: list[tuple[str, str | None]] = []
    monkeypatch.setattr(claude_accounts, "switch", lambda label: calls.append(("switch", label)))
    monkeypatch.setattr(claude_accounts, "set_pinned", lambda label: calls.append(("pin", label)))
    restarts: list[bool] = []
    monkeypatch.setattr(routes, "_schedule_service_restart", lambda: restarts.append(True))

    res = client.post("/api/claude-account/switch", json={"label": "work"})
    assert res.status_code == 200
    assert res.json() == {"ok": True, "label": "work", "restarting": True, "pinned": True}
    # 先切換、切換成功才釘選（手動切換＝進入手動模式，凍結自動輪替）
    assert calls == [("switch", "work"), ("pin", "work")]
    assert restarts == [True]  # 重啟被排程一次


def test_unpin_200_clears_pin(client, monkeypatch):
    """DELETE /api/claude-account/pin＝回自動模式：set_pinned(None)，無需重啟。"""
    pinned: list[str | None] = ["sentinel"]
    monkeypatch.setattr(claude_accounts, "set_pinned", lambda label: pinned.append(label))
    restarts: list[bool] = []
    monkeypatch.setattr(routes, "_schedule_service_restart", lambda: restarts.append(True))

    res = client.delete("/api/claude-account/pin")
    assert res.status_code == 200
    assert res.json() == {"ok": True, "pinned": None}
    assert pinned[-1] is None
    assert restarts == []  # 解除釘選不重啟（輪替下輪自然接手）
