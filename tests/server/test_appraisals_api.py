"""測試 GET /api/appraisals（考核總覽：聚合 summary + 最近 N 筆；讀權限）。

鏡射 tests/server/test_autopilot_activity.py 的 app/client/state 範式：
資料經 studio.appraisal store 落檔（monkeypatch APPRAISALS_FILE 指向 tmp），
驗證回應形狀、limit 夾範圍與門禁（門禁啟用未登入 → 401）。
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from studio import appraisal, config

LOOPBACK_PEER = ("127.0.0.1", 12345)


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
    monkeypatch.setattr(config, "APPRAISALS_FILE", tmp_path / "appraisals.json")
    return tmp_path


def _seed() -> None:
    now = time.time()
    appraisal.record(
        [
            {
                "session_id": "s1",
                "task_id": 1,
                "role": "engineer",
                "provider": "claude",
                "model": "claude-opus-4-8",
                "score": 5,
                "comment": "穩定高質量",
                "objective": {"qa_rounds": 1, "qa_passed": True},
                "created_at": now - 60,
            },
            {
                "session_id": "s1",
                "task_id": 2,
                "role": "engineer",
                "provider": "claude",
                "model": "claude-opus-4-8",
                "score": 4,
                "comment": "小幅返工",
                "objective": {"qa_rounds": 2, "qa_passed": False},
                "created_at": now - 30,
            },
            {
                "session_id": "s2",
                "task_id": None,
                "role": "",
                "provider": "codex",
                "model": "",
                "score": 3,
                "comment": "速度偏慢",
                "objective": {},
                "created_at": now,
            },
        ]
    )


def test_appraisals_returns_summary_and_recent(client, state):
    _seed()
    data = client.get("/api/appraisals").json()
    # 聚合層：per provider 平均分/樣本數/QA 通過率（無客觀裁決樣本＝null）。
    provs = data["summary"]["providers"]
    assert provs["claude"] == {"avg_score": 4.5, "n": 2, "pass_rate": 0.5}
    assert provs["codex"] == {"avg_score": 3.0, "n": 1, "pass_rate": None}
    # 第二層：provider/model（model 空者不入）。
    assert data["summary"]["models"]["claude/claude-opus-4-8"]["n"] == 2
    # 最近紀錄：由新到舊、欄位齊備。
    recents = data["recent"]
    assert [r["provider"] for r in recents] == ["codex", "claude", "claude"]
    assert recents[1]["comment"] == "小幅返工" and recents[1]["score"] == 4
    assert recents[1]["objective"]["qa_passed"] is False


def test_appraisals_limit_clamped(client, state):
    _seed()
    data = client.get("/api/appraisals", params={"limit": 1}).json()
    assert len(data["recent"]) == 1 and data["recent"][0]["provider"] == "codex"
    # limit=0/負值被夾回至少 1 筆，不致回空或炸範圍。
    data = client.get("/api/appraisals", params={"limit": 0}).json()
    assert len(data["recent"]) == 1


def test_appraisals_empty_store(client, state):
    data = client.get("/api/appraisals").json()
    assert data == {"summary": {"providers": {}, "models": {}}, "recent": []}


def test_appraisals_requires_auth(client, state, monkeypatch):
    monkeypatch.setattr(config, "ACCESS_PASSWORD", "secret")  # 門禁啟用、未登入
    assert client.get("/api/appraisals").status_code == 401
