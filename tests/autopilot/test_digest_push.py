"""每日摘要推播(軌 D3):旗標關=零推播;開=落盤後推一則三行摘要(kind=daily_digest)。"""

from __future__ import annotations

import pytest

from studio import autopilot, config


def _fake_env(monkeypatch, tmp_path, *, push):
    monkeypatch.setattr(config, "AUTOPILOT_STATE_DIR", tmp_path / "ap")
    (tmp_path / "ap").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(config, "DIGEST_PUSH", push)
    sent = []
    monkeypatch.setattr(
        autopilot.notify, "send_bg", lambda kind, title, **kw: sent.append((kind, kw))
    )
    monkeypatch.setattr(autopilot.digest, "list_digests", lambda: [])
    monkeypatch.setattr(autopilot.digest, "save_digest", lambda: "digest-x.md")
    monkeypatch.setattr(
        autopilot.digest,
        "build_digest",
        lambda days=7: {
            "completion_rate": 0.8,
            "trust": {"zero_touch_rate": 0.95},
            "backlog_counts": {"pending": 7},
            "prs": [{"pr": 1}, {"pr": 2}],
        },
    )
    return sent


@pytest.mark.asyncio
async def test_digest_push_disabled_by_default(monkeypatch, tmp_path):
    sent = _fake_env(monkeypatch, tmp_path, push=False)

    async def one_cycle():
        # 只跑一輪:攔 sleep 丟 Cancelled
        async def stop(_s):
            raise __import__("asyncio").CancelledError()

        monkeypatch.setattr(autopilot.asyncio, "sleep", stop)
        with pytest.raises(__import__("asyncio").CancelledError):
            await autopilot._digest_scheduler()

    await one_cycle()
    assert sent == [], "旗標關=零推播"


@pytest.mark.asyncio
async def test_digest_push_enabled_sends_summary(monkeypatch, tmp_path):
    sent = _fake_env(monkeypatch, tmp_path, push=True)

    async def stop(_s):
        raise __import__("asyncio").CancelledError()

    monkeypatch.setattr(autopilot.asyncio, "sleep", stop)
    with pytest.raises(__import__("asyncio").CancelledError):
        await autopilot._digest_scheduler()
    assert [k for k, _ in sent] == ["daily_digest"]
    kw = sent[0][1]
    assert (
        kw["rate"] == "80%"
        and kw["zero_touch"] == "95%"
        and kw["pending"] == 7
        and kw["merged"] == 2
    )
