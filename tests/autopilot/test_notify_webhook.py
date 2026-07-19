"""QA guard: notification webhook behavior must be non-blocking and secret-safe."""

from __future__ import annotations

import json
import logging

import pytest

from studio import config, notify


@pytest.fixture
def notify_env(monkeypatch):
    with monkeypatch.context() as env:
        env.delenv("TI_NOTIFY_WEBHOOK", raising=False)
        env.delenv("TI_NOTIFY_TIMEOUT", raising=False)
        config.reload()
        yield env
    config.reload()


class _Response:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def _sync_thread_class(threads):
    class SyncThread:
        def __init__(self, target, args=(), kwargs=None, daemon=False, **_ignored):
            self.target = target
            self.args = args
            self.kwargs = {} if kwargs is None else kwargs
            self.daemon = daemon
            self.started = False
            threads.append(self)

        def start(self):
            self.started = True
            self.target(*self.args, **self.kwargs)

    return SyncThread


def test_send_bg_without_webhook_starts_no_thread_and_hits_no_network(notify_env, monkeypatch):
    class NoThread:
        def __init__(self, *_args, **_kwargs):
            pytest.fail("send_bg must not start a thread without a webhook")

    monkeypatch.setattr(notify.threading, "Thread", NoThread)
    monkeypatch.setattr(
        notify.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: pytest.fail("send_bg must not hit network without a webhook"),
    )

    notify.send_bg("noop", "disabled", task_id="t1")


def test_send_bg_posts_json_payload_in_daemon_thread(notify_env, monkeypatch):
    webhook = "https://hooks.example.test/notify"
    threads = []
    calls = []

    def fake_urlopen(request, timeout):
        calls.append(
            {
                "url": request.get_full_url(),
                "timeout": timeout,
                "body": json.loads(request.data.decode("utf-8")),
            }
        )
        return _Response()

    notify_env.setenv("TI_NOTIFY_WEBHOOK", webhook)
    notify_env.setenv("TI_NOTIFY_TIMEOUT", "2.25")
    config.reload()
    monkeypatch.setattr(notify.threading, "Thread", _sync_thread_class(threads))
    monkeypatch.setattr(notify.urllib.request, "urlopen", fake_urlopen)

    notify.send_bg("daily_pr_budget_pause", "budget reached", task_id=7, budget=2)

    assert len(threads) == 1
    assert threads[0].daemon is True
    assert threads[0].started is True
    assert calls == [
        {
            "url": webhook,
            "timeout": 2.25,
            "body": {
                "event": "daily_pr_budget_pause",
                "message": "budget reached",
                "payload": {"task_id": 7, "budget": 2},
            },
        }
    ]


def test_send_bg_swallows_webhook_urlopen_errors(notify_env, monkeypatch):
    threads = []

    def fake_urlopen(*_args, **_kwargs):
        raise OSError("network down")

    notify_env.setenv("TI_NOTIFY_WEBHOOK", "https://hooks.example.test/notify")
    config.reload()
    monkeypatch.setattr(notify.threading, "Thread", _sync_thread_class(threads))
    monkeypatch.setattr(notify.urllib.request, "urlopen", fake_urlopen)

    notify.send_bg("event", "message", extra="value")

    assert len(threads) == 1
    assert threads[0].started is True


def test_webhook_url_is_not_logged_when_delivery_fails(notify_env, monkeypatch, caplog):
    webhook = "https://hooks.example.test/notify?token=secret-token"
    threads = []

    def fake_urlopen(*_args, **_kwargs):
        raise RuntimeError(f"failed posting to {webhook}")

    notify_env.setenv("TI_NOTIFY_WEBHOOK", webhook)
    config.reload()
    caplog.set_level(logging.DEBUG, logger="ti.notify")
    monkeypatch.setattr(notify.threading, "Thread", _sync_thread_class(threads))
    monkeypatch.setattr(notify.urllib.request, "urlopen", fake_urlopen)

    notify.send_bg("event", "message", extra="value")

    assert len(threads) == 1
    assert webhook not in caplog.text
