"""QA regression tests for notify webhook failure boundaries."""

from __future__ import annotations

import json

import pytest

from studio import config, notify


@pytest.fixture(autouse=True)
def _isolated_notify_state(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_STATE_DIR", tmp_path / "ap")
    monkeypatch.setattr(config, "NOTIFY_WEBHOOK", "")
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "")
    monkeypatch.setattr(config, "NOTIFY_TIMEOUT", 10.0)
    return tmp_path


def test_webhook_payload_redacts_secrets_before_network(monkeypatch):
    webhook = "https://hooks.example.test/secret-token"
    captured: dict[str, object] = {}

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        captured["headers"] = dict(request.header_items())
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return _Response()

    monkeypatch.setattr(config, "NOTIFY_WEBHOOK", webhook)
    monkeypatch.setattr(config, "NOTIFY_TIMEOUT", 4.25)
    monkeypatch.setattr(notify.urllib.request, "urlopen", fake_urlopen)

    assert notify.send(
        "task_failed",
        f"失敗於 /root/private/repo；hook={webhook}",
        api_token="top-secret",
        webhook_url=webhook,
        nested={"password": "pw-value", "path": "/var/tmp/leak.txt"},
        notes=["see /home/me/key", "safe"],
    )

    encoded_body = json.dumps(captured["body"], ensure_ascii=False)
    assert captured["url"] == webhook
    assert captured["timeout"] == 4.25
    assert captured["headers"]["Content-type"] == "application/json"
    assert "top-secret" not in encoded_body
    assert "pw-value" not in encoded_body
    assert webhook not in encoded_body
    assert "/root/private" not in encoded_body
    assert "/var/tmp" not in encoded_body
    assert "/home/me" not in encoded_body
    assert captured["body"]["api_token"] == {"configured": True}
    assert captured["body"]["webhook_url"] == {"configured": True}


def test_send_bg_without_any_sink_records_delivery_without_thread_or_network(monkeypatch):
    class NoThread:
        def __init__(self, *_args, **_kwargs):
            pytest.fail("no sink configured must not start a notification thread")

    monkeypatch.setattr(notify.threading, "Thread", NoThread)
    monkeypatch.setattr(
        notify.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: pytest.fail("no sink configured must not hit network"),
    )

    notify.send_bg("task_failed", "no sink", task_id=123)

    events = notify.read_events(1)
    deliveries = notify.read_deliveries(1)
    assert len(events) == 1
    assert events[0]["kind"] == "task_failed"
    assert len(deliveries) == 1
    assert deliveries[0]["alert_event_id"] == events[0]["event_id"]
    assert deliveries[0]["sink"] == "none"
    assert deliveries[0]["ok"] is False
    assert deliveries[0]["error"] == "not_configured"
