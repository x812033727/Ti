"""QA 加固測試：任務 #2 —— TI_PUBLISH_MERGE 開關 + _maybe_publish 接線。

涵蓋：
- 旗標預設關閉、向後相容；env=1/0/未設定的解析（透過 config.reload）。
- settings.py：TI_PUBLISH_MERGE 在 FIELDS/ALLOWED、為 select 0/1、read 回報、update 寫入並 reload、拒絕非法值。
- _maybe_publish：PUBLISH_AUTO 關閉不發佈；開啟時以 merge=config.PUBLISH_MERGE 呼叫 publish。
"""

from __future__ import annotations

import os

import pytest

from studio import config, orchestrator, publisher, settings


# --- config 旗標 -----------------------------------------------------
@pytest.fixture
def env_restore(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    saved = os.environ.get("TI_PUBLISH_MERGE")
    yield
    if saved is None:
        os.environ.pop("TI_PUBLISH_MERGE", None)
    else:
        os.environ["TI_PUBLISH_MERGE"] = saved
    config.reload()


def test_flag_default_off(env_restore, monkeypatch):
    monkeypatch.delenv("TI_PUBLISH_MERGE", raising=False)
    config.reload()
    assert config.PUBLISH_MERGE is False  # 未設定＝關閉（向後相容）


@pytest.mark.parametrize(
    "val,expected",
    [("1", True), ("0", False), ("true", True), ("false", False), ("", False)],
)
def test_flag_parsing(env_restore, monkeypatch, val, expected):
    monkeypatch.setenv("TI_PUBLISH_MERGE", val)
    config.reload()
    assert config.PUBLISH_MERGE is expected


# --- settings.py 接線 ------------------------------------------------
def test_field_registered():
    f = {x.env: x for x in settings.FIELDS}.get("TI_PUBLISH_MERGE")
    assert f is not None
    assert f.kind == "select" and f.options == ("0", "1")
    assert "TI_PUBLISH_MERGE" in settings.ALLOWED


def test_read_reports_flag(env_restore, monkeypatch):
    monkeypatch.setenv("TI_PUBLISH_MERGE", "1")
    fields = {x["env"]: x for x in settings.read()["fields"]}
    assert fields["TI_PUBLISH_MERGE"]["value"] == "1"


def test_update_writes_and_reloads(env_restore):
    settings.update({"TI_PUBLISH_MERGE": "1"})
    assert config.PUBLISH_MERGE is True
    env_text = (config.PROJECT_ROOT / ".env").read_text()
    assert "TI_PUBLISH_MERGE" in env_text
    settings.update({"TI_PUBLISH_MERGE": "0"})
    assert config.PUBLISH_MERGE is False


def test_update_rejects_bad_value(env_restore, monkeypatch):
    monkeypatch.setenv("TI_PUBLISH_MERGE", "0")
    settings.update({"TI_PUBLISH_MERGE": "2"})  # 非 0/1 應被忽略
    assert os.environ["TI_PUBLISH_MERGE"] == "0"


# --- _maybe_publish 接線 ---------------------------------------------
def _make_orch():
    async def broadcast(_event):
        return None

    o = orchestrator.StudioSession("sess-qa", broadcast, cwd="/tmp/ws")
    o._requirement = "需求"
    return o


@pytest.mark.asyncio
async def test_maybe_publish_skips_when_auto_off(monkeypatch):
    monkeypatch.setattr(config, "PUBLISH_AUTO", False)
    monkeypatch.setattr(publisher, "is_configured", lambda: True)
    called = {"n": 0}

    async def spy_publish(*a, **k):
        called["n"] += 1

    monkeypatch.setattr(publisher, "publish", spy_publish)
    await _make_orch()._maybe_publish(done=True)
    assert called["n"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("flag", [True, False])
async def test_maybe_publish_passes_merge_flag(monkeypatch, flag):
    monkeypatch.setattr(config, "PUBLISH_AUTO", True)
    monkeypatch.setattr(config, "PUBLISH_MERGE", flag)
    monkeypatch.setattr(publisher, "is_configured", lambda: True)
    seen = {}

    async def spy_publish(cwd, session_id, requirement, *, merge=False):
        seen["merge"] = merge
        return publisher.PublishResult(True, "ok")

    monkeypatch.setattr(publisher, "publish", spy_publish)
    await _make_orch()._maybe_publish(done=True)
    assert seen["merge"] is flag


@pytest.mark.asyncio
async def test_maybe_publish_skips_when_not_done(monkeypatch):
    monkeypatch.setattr(config, "PUBLISH_AUTO", True)
    monkeypatch.setattr(publisher, "is_configured", lambda: True)
    called = {"n": 0}

    async def spy_publish(*a, **k):
        called["n"] += 1

    monkeypatch.setattr(publisher, "publish", spy_publish)
    await _make_orch()._maybe_publish(done=False)  # 未完成不發佈
    assert called["n"] == 0
