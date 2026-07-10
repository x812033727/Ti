"""測試 /api/autopilot/activity 動態視圖聚合與 /api/autopilot/triage 分診端點（含權限）。

activity：backlog 全部任務 updated_at 倒序 + 落檔欄位（pr/merged_branch/deploy_msg）+
有 session_id 者併 history meta 的 scorecard/token_usage；limit 分頁。
triage：走 WRITE_DEPS（require_admin）——門禁停用限本機、門禁啟用需登入。
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from studio import backlog, config, history

LOOPBACK_PEER = ("127.0.0.1", 12345)
PUBLIC_PEER = ("203.0.113.5", 40000)


@pytest.fixture
def app():
    from studio.server import app as fastapi_app

    return fastapi_app


@pytest.fixture
def client(app):
    return TestClient(app, client=LOOPBACK_PEER)


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ACCESS_PASSWORD", "")  # 門禁停用 → require_auth 放行
    monkeypatch.setattr(config, "AUTOPILOT_STATE_DIR", tmp_path / "ap")
    monkeypatch.setattr(config, "HISTORY_ROOT", tmp_path / "hist")
    monkeypatch.setattr(config, "AUTOPILOT_TASK_MAX_ATTEMPTS", 3)
    return tmp_path


def _patch_task(task_id: int, **fields) -> None:
    """直接改 backlog.json 欄位（固定 updated_at 讓排序斷言確定性）。"""
    p = backlog._path(None)
    data = json.loads(p.read_text(encoding="utf-8"))
    for t in data["tasks"]:
        if t["id"] == task_id:
            t.update(fields)
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _seed_session_meta(sid: str, *, ttft_s: float | None = None) -> None:
    """造一個帶 scorecard 與 token_usage 的 history meta。"""
    meta = history.start_session(sid, "[autopilot] 測試任務")
    meta["status"] = "completed"
    meta["scorecard"] = {"tasks_total": 2, "tasks_done": 2, "qa_total": 3, "qa_pass": 3}
    meta["token_usage"] = {
        "total": {"prompt": 100, "completion": 50, "total": 150, "cost_usd": 0.5, "calls": 2},
        "by_provider": {"claude": {"total": 150}},
        "by_model": {"claude-opus-4-8": {"total": 150}},
        "by_role": {"senior": {"total": 150}},
    }
    if ttft_s is not None:
        meta["token_usage"]["ttft_s"] = ttft_s
    history._write_meta(sid, meta)


def _seed_tasks() -> dict[str, int]:
    """三筆任務：done（含 PR/部署落檔＋session meta）、failed、pending。回傳 id 對照。"""
    done = backlog.add("已完成的任務")
    failed = backlog.add("失敗的任務")
    pending = backlog.add("排隊中的任務")
    _seed_session_meta("s-done", ttft_s=0.123)
    backlog.set_status(
        done["id"],
        "done",
        session_id="s-done",
        pr=101,
        merged_branch="autopilot/task-1",
        deploy_msg="重佈成功：abc12345 → def67890",
    )
    backlog.set_status(failed["id"], "failed", note="討論未達完成", session_id="s-ghost")
    # 固定 updated_at：pending 最新、done 次之、failed 最舊，排序斷言不依賴時鐘。
    _patch_task(done["id"], updated_at=200.0)
    _patch_task(failed["id"], updated_at=100.0)
    _patch_task(pending["id"], updated_at=300.0)
    return {"done": done["id"], "failed": failed["id"], "pending": pending["id"]}


# --- activity 聚合 -----------------------------------------------------------


def test_activity_aggregates_and_sorts_desc(client, state):
    ids = _seed_tasks()
    data = client.get("/api/autopilot/activity").json()
    assert data["total"] == 3
    rows = data["tasks"]
    assert [r["id"] for r in rows] == [ids["pending"], ids["done"], ids["failed"]]  # 倒序
    by_id = {r["id"]: r for r in rows}
    done = by_id[ids["done"]]
    # backlog 落檔欄位
    assert done["pr"] == 101
    assert done["merged_branch"] == "autopilot/task-1"
    assert "abc12345 → def67890" in done["deploy_msg"]
    assert done["session_id"] == "s-done"
    # history meta 併入：scorecard + token_usage（per-provider/model）
    assert done["scorecard"]["tasks_done"] == 2
    assert done["token_usage"]["by_provider"]["claude"]["total"] == 150
    assert "claude-opus-4-8" in done["token_usage"]["by_model"]
    assert done["token_usage"]["ttft_s"] == pytest.approx(0.123)
    # per-task 成本（第五輪 F4）：total 桶的 cost_usd 提升為 timeline 穩定欄位
    assert done["token_usage"]["cost_usd"] == pytest.approx(0.5)
    # failed 任務：note/attempts/source 齊備；session meta 不存在 → 不虛構聚合欄位
    failed = by_id[ids["failed"]]
    assert failed["note"] == "討論未達完成"
    assert "scorecard" not in failed and "token_usage" not in failed
    assert failed["source"] == "seed" and failed["attempts"] == 0


def test_activity_limit_pagination(client, state):
    ids = _seed_tasks()
    data = client.get("/api/autopilot/activity", params={"limit": 1}).json()
    assert data["total"] == 3
    assert len(data["tasks"]) == 1
    assert data["tasks"][0]["id"] == ids["pending"]  # 只回最新一筆


def test_activity_empty_backlog(client, state):
    data = client.get("/api/autopilot/activity").json()
    assert data == {"tasks": [], "total": 0}


def test_activity_omits_legacy_ttft_s_when_missing(client, state):
    meta = history.start_session("s-legacy", "[autopilot] 舊任務")
    meta["status"] = "completed"
    meta["scorecard"] = {"tasks_total": 1, "tasks_done": 1, "qa_total": 0, "qa_pass": 0}
    meta["token_usage"] = {
        "total": {"prompt": 10, "completion": 2, "total": 12, "cost_usd": 0.0, "calls": 1},
        "by_provider": {"claude": {"total": 12}},
        "by_model": {"claude-sonnet": {"total": 12}},
        "by_role": {"senior": {"total": 12}},
    }
    history._write_meta("s-legacy", meta)
    task = backlog.add("舊任務")
    backlog.set_status(task["id"], "done", session_id="s-legacy")

    data = client.get("/api/autopilot/activity").json()
    rows = {r["session_id"]: r for r in data["tasks"] if r.get("session_id")}
    legacy = rows["s-legacy"]
    assert "ttft_s" not in legacy["token_usage"]


def test_activity_requires_auth(client, state, monkeypatch):
    monkeypatch.setattr(config, "ACCESS_PASSWORD", "secret")  # 門禁啟用、未登入
    assert client.get("/api/autopilot/activity").status_code == 401


# --- triage 端點 -------------------------------------------------------------


def test_triage_endpoint_retries_infra_failure(client, state):
    t = backlog.add("逾時任務")
    backlog.set_status(t["id"], "failed", note="(逾時 600s)", attempts=1)
    resp = client.post("/api/autopilot/triage")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True and body["retried"] == 1 and body["parked"] == 0
    assert backlog.list_tasks("pending")[0]["id"] == t["id"]


def test_triage_blocked_for_public_peer_when_auth_disabled(app, state):
    """門禁停用時 fail-safe 限本機：外網來源 403，backlog 不被改動。"""
    t = backlog.add("逾時任務")
    backlog.set_status(t["id"], "failed", note="(逾時 600s)")
    public = TestClient(app, client=PUBLIC_PEER)
    assert public.post("/api/autopilot/triage").status_code == 403
    assert backlog.list_tasks("failed")[0]["id"] == t["id"]  # 狀態未變


def test_triage_requires_login_when_auth_enabled(client, state, monkeypatch):
    monkeypatch.setattr(config, "ACCESS_PASSWORD", "secret")
    assert client.post("/api/autopilot/triage").status_code == 401
