from __future__ import annotations

import urllib.request

from studio import autopilot, config, notify


def test_autopilot_core_route_records_digest_event_without_push(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(config, "AUTOPILOT_EVAL_MEMORY", 20)
    monkeypatch.setattr(config, "NOTIFY_WEBHOOK", "https://hook.example/ti")
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "")
    monkeypatch.setattr(autopilot.backlog, "recent_done_titles", lambda _limit: {"已完成核心改動"})

    pushed = []
    monkeypatch.setattr(urllib.request, "urlopen", lambda *args, **kwargs: pushed.append(args))

    def fake_enqueue(items, *, source="core"):
        assert source == "intent"
        assert [item["title"] for item in items] == ["新核心改動"]
        return len(items)

    monkeypatch.setattr(autopilot, "_enqueue_core_items", fake_enqueue)

    routed = autopilot._route_core_changes(
        [
            {"title": "已完成核心改動", "priority": 0, "type": "bug"},
            {"title": "新核心改動", "priority": 0, "type": "bug"},
        ],
        source="intent",
    )

    assert routed == 1
    assert pushed == []
    rec = notify.read_events(1)[-1]
    assert rec["kind"] == "core_change_routed"
    assert rec["source"] == "intent"
    assert rec["proposed"] == 2
    assert rec["routed"] == 1
    assert rec["skipped_done"] == 1
    assert rec["skipped_existing_or_invalid"] == 0
    assert rec["titles"] == ["已完成核心改動", "新核心改動"]
