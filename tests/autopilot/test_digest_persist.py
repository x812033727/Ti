"""digest 落盤與歷史（第五輪 F6）：save/list/read + autopilot 每日排程器。

守護不變量：
- save_digest 以 UTC 日期命名、同日重呼叫冪等覆寫；
- list_digests 新→舊、忽略不合名檔；read_digest 檔名白名單擋路徑穿越；
- _digest_scheduler 當日檔缺才寫、已存在跳過；例外不冒泡。
"""

from __future__ import annotations

import asyncio
import time

import pytest

from studio import autopilot, config, digest


@pytest.fixture(autouse=True)
def _state(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_STATE_DIR", tmp_path / "ap")
    (tmp_path / "ap").mkdir(parents=True)
    import studio.backlog as backlog_mod
    import studio.lessons as lessons_mod

    monkeypatch.setattr(backlog_mod, "_read_cache", {}, raising=False)
    monkeypatch.setattr(lessons_mod, "_path", lambda: tmp_path / "lessons.json")
    monkeypatch.setattr(lessons_mod, "_read_cache", {}, raising=False)
    return tmp_path


def test_save_digest_utc_named_and_idempotent(tmp_path):
    name = digest.save_digest(now=0)  # epoch=1970-01-01（UTC）
    assert name == "digest-1970-01-01.md"
    p = tmp_path / "ap" / "digests" / name
    first = p.read_text(encoding="utf-8")
    assert "Ti 週報" in first
    assert digest.save_digest(now=0) == name, "同日重呼叫冪等覆寫"


def test_list_and_read_digests(tmp_path):
    digest.save_digest(now=0)
    digest.save_digest(now=86400)
    (tmp_path / "ap" / "digests" / "not-a-digest.txt").write_text("x", encoding="utf-8")
    names = [d["name"] for d in digest.list_digests()]
    assert names == ["digest-1970-01-02.md", "digest-1970-01-01.md"], "新→舊且忽略不合名檔"
    assert "Ti 週報" in digest.read_digest("digest-1970-01-01.md")


@pytest.mark.parametrize("bad", ["../../../etc/passwd", "digest-1970-01-01.md/../x", "", "a.md"])
def test_read_digest_rejects_traversal(bad):
    assert digest.read_digest(bad) is None


def test_read_digest_missing_returns_none():
    assert digest.read_digest("digest-2099-01-01.md") is None


async def _run_scheduler_once(monkeypatch):
    ticks = {"n": 0}

    async def fast_sleep(_s):
        ticks["n"] += 1
        if ticks["n"] > 1:
            raise asyncio.CancelledError()

    monkeypatch.setattr(autopilot.asyncio, "sleep", fast_sleep)
    with pytest.raises(asyncio.CancelledError):
        await autopilot._digest_scheduler()


@pytest.mark.asyncio
async def test_scheduler_writes_when_missing(monkeypatch, tmp_path):
    await _run_scheduler_once(monkeypatch)
    today = f"digest-{time.strftime('%Y-%m-%d', time.gmtime())}.md"
    assert (tmp_path / "ap" / "digests" / today).is_file()


@pytest.mark.asyncio
async def test_scheduler_skips_when_exists(monkeypatch, tmp_path):
    today = digest.save_digest()
    p = tmp_path / "ap" / "digests" / today
    p.write_text("已存在的內容", encoding="utf-8")
    await _run_scheduler_once(monkeypatch)
    assert p.read_text(encoding="utf-8") == "已存在的內容", "當日已存在不得覆寫"


@pytest.mark.asyncio
async def test_scheduler_survives_failure(monkeypatch):
    monkeypatch.setattr(digest, "list_digests", lambda: (_ for _ in ()).throw(RuntimeError("x")))
    await _run_scheduler_once(monkeypatch)  # 不拋（CancelledError 除外）即通過
