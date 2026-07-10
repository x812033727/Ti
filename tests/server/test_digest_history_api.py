"""digest 歷史 API（第五輪 F6）：GET /api/autopilot/digests（清單）與 /{name}（內容）。

守護不變量：清單新→舊；讀取回 markdown；檔名不合白名單/不存在一律 404（擋路徑穿越）。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from studio import config, digest


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ACCESS_PASSWORD", "")
    monkeypatch.setattr(config, "AUTOPILOT_STATE_DIR", tmp_path)
    import studio.backlog as backlog_mod
    import studio.lessons as lessons_mod

    monkeypatch.setattr(backlog_mod, "_read_cache", {}, raising=False)
    monkeypatch.setattr(lessons_mod, "_path", lambda: tmp_path / "lessons.json")
    monkeypatch.setattr(lessons_mod, "_read_cache", {}, raising=False)
    from studio.server import app

    return TestClient(app)


def test_digests_list_and_read(client):
    digest.save_digest(now=0)
    digest.save_digest(now=86400)
    data = client.get("/api/autopilot/digests").json()
    assert [d["name"] for d in data["digests"]] == [
        "digest-1970-01-02.md",
        "digest-1970-01-01.md",
    ]
    one = client.get("/api/autopilot/digests/digest-1970-01-01.md").json()
    assert one["name"] == "digest-1970-01-01.md" and "Ti 週報" in one["markdown"]


def test_digests_empty_and_404(client):
    assert client.get("/api/autopilot/digests").json() == {"digests": []}
    assert client.get("/api/autopilot/digests/digest-2099-01-01.md").status_code == 404
    assert client.get("/api/autopilot/digests/..%2F..%2Fetc%2Fpasswd").status_code == 404
