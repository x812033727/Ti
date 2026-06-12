"""/ws × repo_base 接線：專案模式同步工作基底、一次性不碰、fatal 收尾乾淨。

以 spy 頂替 repo_base.ensure_base（不碰 git/網路），驗證 ws 的呼叫接線與
base_repo 旗標傳遞（SESSION_STARTED.payload.base_repo 僅在確實同步自目標 repo 時帶值）。
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from studio import config, projects, repo_base, ws


class EnsureSpy:
    def __init__(self, result: repo_base.SyncResult):
        self.result = result
        self.calls: list[dict] = []

    async def __call__(self, cwd, repo, *, broadcast=None, session_id=""):
        self.calls.append({"cwd": Path(cwd), "repo": repo, "session_id": session_id})
        return self.result


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ACCESS_PASSWORD", "")
    monkeypatch.setattr(config, "OFFLINE_MODE", True)
    monkeypatch.setattr(config, "OFFLINE_DELAY", 0.0)
    monkeypatch.setattr(config, "DEBATE_ROUNDS", 1)
    monkeypatch.setattr(config, "PROJECTS_ROOT", tmp_path / "projects")
    monkeypatch.setattr(config, "WORKSPACE_ROOT", tmp_path / "ws")
    monkeypatch.setattr(config, "HISTORY_ROOT", tmp_path / "hist")
    # 全域目標 repo 固定值，讓 fallback 行為與真實 .env 解耦、測試可重現。
    monkeypatch.setattr(config, "PUBLISH_REPO", "global/outputs")
    monkeypatch.setattr(ws, "_active_sessions", 0)
    monkeypatch.setattr(ws, "_active_projects", set())
    from studio.server import app

    return TestClient(app, client=("127.0.0.1", 12345))


def _drain(conn, until=("done", "error"), limit=3000) -> list[dict]:
    events = []
    for _ in range(limit):
        ev = conn.receive_json()
        events.append(ev)
        if ev["type"] in until:
            break
    return events


def _session_started(events: list[dict]) -> dict:
    started = [e for e in events if e["type"] == "session_started"]
    assert started, f"未收到 session_started：{[e['type'] for e in events]}"
    return started[0]["payload"]


def test_project_session_syncs_base_and_flags_prompt(env, monkeypatch):
    """專案模式：ensure_base 以專案 workspace 為 cwd 被呼叫；based → 事件帶 base_repo。"""
    spy = EnsureSpy(repo_base.SyncResult("cloned", "已以目標 repo 為基底"))
    monkeypatch.setattr(repo_base, "ensure_base", spy)
    pid = projects.create("產品X")["id"]
    projects.set_publish_repo(pid, "me/product")

    with env.websocket_connect("/ws") as conn:
        conn.send_json({"requirement": "加個功能", "project_id": pid})
        events = _drain(conn)

    assert len(spy.calls) == 1
    assert spy.calls[0]["repo"] == "me/product"
    assert spy.calls[0]["cwd"] == projects.workspace_dir(pid)
    assert _session_started(events)["base_repo"] == "me/product"


def test_project_session_diverged_does_not_claim_base(env, monkeypatch):
    """同步結果非 based（如 diverged）：不得對 session 宣告「基底在目錄裡」。"""
    spy = EnsureSpy(repo_base.SyncResult("diverged", "無共同歷史"))
    monkeypatch.setattr(repo_base, "ensure_base", spy)
    pid = projects.create("產品Y")["id"]
    projects.set_publish_repo(pid, "me/other")

    with env.websocket_connect("/ws") as conn:
        conn.send_json({"requirement": "加個功能", "project_id": pid})
        events = _drain(conn)

    assert len(spy.calls) == 1
    assert _session_started(events)["base_repo"] is None


def test_project_without_repo_falls_back_to_global(env, monkeypatch):
    """專案沒自設 publish_repo：工作基底退回全域 TI_PUBLISH_REPO（修正『自己做自己』）。"""
    spy = EnsureSpy(repo_base.SyncResult("cloned", "已以全域 repo 為基底"))
    monkeypatch.setattr(repo_base, "ensure_base", spy)
    pid = projects.create("沒設 repo 的產品")["id"]  # 不呼叫 set_publish_repo

    with env.websocket_connect("/ws") as conn:
        conn.send_json({"requirement": "加個功能", "project_id": pid})
        events = _drain(conn)

    assert len(spy.calls) == 1
    assert spy.calls[0]["repo"] == "global/outputs"
    assert _session_started(events)["base_repo"] == "global/outputs"


def test_oneoff_session_bases_on_global_repo(env, monkeypatch):
    """一次性 session（無專案、無 repo_url）：也以全域目標 repo 為工作基底。"""
    spy = EnsureSpy(repo_base.SyncResult("cloned", "已以全域 repo 為基底"))
    monkeypatch.setattr(repo_base, "ensure_base", spy)

    with env.websocket_connect("/ws") as conn:
        conn.send_json({"requirement": "做一個 BMI CLI"})
        events = _drain(conn)

    assert len(spy.calls) == 1
    assert spy.calls[0]["repo"] == "global/outputs"
    assert _session_started(events)["base_repo"] == "global/outputs"


def test_fatal_sync_aborts_and_releases(env, monkeypatch):
    """全新 workspace 拿不到基底（fatal）：回明確錯誤、不開工，slot 與專案互斥釋放。"""
    spy = EnsureSpy(repo_base.SyncResult("error", "無法取得目標 repo"))
    monkeypatch.setattr(repo_base, "ensure_base", spy)
    pid = projects.create("產品Z")["id"]
    projects.set_publish_repo(pid, "me/product")

    with env.websocket_connect("/ws") as conn:
        conn.send_json({"requirement": "加個功能", "project_id": pid})
        msg = conn.receive_json()

    assert msg["type"] == "error"
    assert "工作基底同步失敗" in msg["payload"]["message"]
    for _ in range(100):  # 收尾在 finally，給極短時間
        if ws._active_sessions == 0 and pid not in ws._active_projects:
            break
        time.sleep(0.02)
    assert ws._active_sessions == 0
    assert pid not in ws._active_projects
