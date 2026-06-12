"""專案發佈 repo 設定 API：格式驗證、清除、404、meta 持久化與 session 接線。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from studio import config, projects


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ACCESS_PASSWORD", "")
    monkeypatch.setattr(config, "PROJECTS_ROOT", tmp_path / "projects")
    monkeypatch.setattr(config, "WORKSPACE_ROOT", tmp_path / "ws")
    from studio.server import app

    return TestClient(app)


def test_set_and_clear_publish_repo(client):
    pid = projects.create("產品X")["id"]
    res = client.post(f"/api/projects/{pid}/publish-repo", json={"repo": "me/product"})
    assert res.status_code == 200
    assert res.json()["project"]["publish_repo"] == "me/product"
    # 持久化＋detail 帶出
    assert projects.get(pid)["publish_repo"] == "me/product"
    assert client.get(f"/api/projects/{pid}").json()["project"]["publish_repo"] == "me/product"
    # 清除
    res = client.post(f"/api/projects/{pid}/publish-repo", json={"repo": ""})
    assert res.status_code == 200 and res.json()["project"]["publish_repo"] == ""


@pytest.mark.parametrize("bad", ["noslash", "a/b/c", "a b/c", "owner/", "/repo", "a;b/c"])
def test_set_publish_repo_rejects_bad_format(client, bad):
    pid = projects.create("格式")["id"]
    res = client.post(f"/api/projects/{pid}/publish-repo", json={"repo": bad})
    assert res.status_code == 400
    assert projects.get(pid).get("publish_repo", "") == ""


def test_set_publish_repo_unknown_project_404(client):
    assert client.post("/api/projects/nope/publish-repo", json={"repo": "a/b"}).status_code == 404


async def test_session_uses_project_publish_repo(tmp_path, monkeypatch):
    """StudioSession 接到 publish_repo 後，_maybe_publish 全程以該 repo 覆寫發佈目標。"""
    from studio import publisher
    from studio.orchestrator import StudioSession

    monkeypatch.setattr(config, "PUBLISH_AUTO", True)
    monkeypatch.setattr(config, "GITHUB_TOKEN", "tok")
    monkeypatch.setattr(config, "PUBLISH_REPO", "")  # 全域未設，靠專案自己的 repo

    seen = {}

    async def fake_publish(cwd, session_id, requirement, *, merge=False, repo=None):
        seen["repo"] = publisher.current_repo()
        return publisher.PublishResult(True, "已 push", pushed=True)

    monkeypatch.setattr(publisher, "publish", fake_publish)

    async def broadcast(ev):
        pass

    session = StudioSession("s1", broadcast, cwd=tmp_path, publish_repo="me/product")
    await session._maybe_publish(True)
    assert seen["repo"] == "me/product"
    # 離開 _maybe_publish 後覆寫已還原
    assert publisher.current_repo() == ""
