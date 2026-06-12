"""中斷恢復 API：POST /api/projects/{id}/recover 的重置語義與防護。

服務重啟／行程被殺後，backlog 會殘留 in_progress、history meta 會永遠卡在 running
（finish_session 沒跑到）。recover 把兩者清乾淨（冪等），讓前端能無痛重啟改良迴圈。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from studio import backlog, config, history, projects, ws


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ACCESS_PASSWORD", "")  # 門禁停用 → require_auth 放行
    monkeypatch.setattr(config, "PROJECTS_ROOT", tmp_path / "projects")
    monkeypatch.setattr(config, "WORKSPACE_ROOT", tmp_path / "ws")
    monkeypatch.setattr(config, "HISTORY_ROOT", tmp_path / "hist")
    from studio.server import app

    return TestClient(app)


def _seed_interrupted_project() -> tuple[str, str]:
    """建專案＋一個卡在 in_progress 的任務與其幽靈 running session。"""
    pid = projects.create("中斷專案")["id"]
    sdir = projects.state_dir(pid)
    t = backlog.add("被中斷的任務", state_dir=sdir)
    backlog.set_status(t["id"], "in_progress", state_dir=sdir, session_id="ghost1")
    history.start_session("ghost1", "[專案 中斷專案] 被中斷的任務")
    history.record_event("ghost1", {"type": "phase_change", "payload": {}})
    return pid, str(sdir)


def test_recover_unknown_project_404(client):
    assert client.post("/api/projects/nope/recover").status_code == 404


def test_recover_rejected_while_loop_active(client, monkeypatch):
    pid, _ = _seed_interrupted_project()
    monkeypatch.setattr(ws, "_active_projects", {pid})
    res = client.post(f"/api/projects/{pid}/recover")
    assert res.status_code == 409
    # 進行中的任務不可被搶著重置
    sdir = projects.state_dir(pid)
    assert backlog.list_tasks("in_progress", state_dir=sdir)


def test_recover_resets_stale_and_marks_ghost_meta(client):
    pid, _ = _seed_interrupted_project()
    sdir = projects.state_dir(pid)
    # 對照組：done / failed 不該被動到
    done = backlog.add("已完成", state_dir=sdir)
    backlog.set_status(done["id"], "done", state_dir=sdir)
    failed = backlog.add("真失敗", state_dir=sdir)
    backlog.set_status(failed["id"], "failed", state_dir=sdir)

    data = client.post(f"/api/projects/{pid}/recover").json()

    assert data["ok"] is True and data["reset"] == 1
    assert data["counts"]["in_progress"] == 0 and data["counts"]["pending"] == 1
    statuses = {t["title"]: t["status"] for t in backlog.list_tasks(state_dir=sdir)}
    assert statuses == {"被中斷的任務": "pending", "已完成": "done", "真失敗": "failed"}
    # 幽靈 meta：running → error，且 n_events 補正（start 時寫 0）
    meta = history.get_meta("ghost1")
    assert meta["status"] == "error" and meta["n_events"] == 1 and meta.get("note")


def test_recover_is_idempotent(client):
    pid, _ = _seed_interrupted_project()
    assert client.post(f"/api/projects/{pid}/recover").json()["reset"] == 1
    again = client.post(f"/api/projects/{pid}/recover").json()
    assert again["ok"] is True and again["reset"] == 0


def test_mark_interrupted_skips_finished_sessions(tmp_path, monkeypatch):
    """已正常收尾的場次不可被覆寫（只清 running 的幽靈）。"""
    monkeypatch.setattr(config, "HISTORY_ROOT", tmp_path / "hist")
    history.start_session("s1", "需求")
    history.record_event("s1", {"type": "done", "payload": {"completed": True}})
    history.finish_session("s1")
    assert history.mark_interrupted("s1", "不該生效") is False
    assert history.get_meta("s1")["status"] == "completed"
    # 不存在的 session 也安全回 False
    assert history.mark_interrupted("no-such", "x") is False
