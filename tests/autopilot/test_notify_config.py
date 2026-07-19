"""QA guard: notification config knobs must stay reload-safe."""

from __future__ import annotations

import ast
import json
import logging
from collections.abc import Iterable
from pathlib import Path

import pytest

from studio import config, notify

REQUIRED_NOTIFY_CONFIG = {"NOTIFY_WEBHOOK", "NOTIFY_TIMEOUT"}


def _assigned_names(statements: Iterable[ast.stmt]) -> set[str]:
    names: set[str] = set()
    for stmt in statements:
        targets: list[ast.expr] = []
        if isinstance(stmt, ast.Assign):
            targets = list(stmt.targets)
        elif isinstance(stmt, ast.AnnAssign):
            targets = [stmt.target]
        elif isinstance(stmt, ast.AugAssign):
            targets = [stmt.target]

        for target in targets:
            if isinstance(target, ast.Name):
                names.add(target.id)
    return names


def _reload_function(tree: ast.Module) -> ast.FunctionDef:
    for stmt in tree.body:
        if isinstance(stmt, ast.FunctionDef) and stmt.name == "reload":
            return stmt
    pytest.fail("studio/config.py must define reload()")


def test_notify_config_is_synchronized_in_all_three_config_sections():
    tree = ast.parse(Path(config.__file__).read_text(encoding="utf-8"))
    reload_fn = _reload_function(tree)

    top_level_assigns = _assigned_names(tree.body)
    reload_globals = {
        name for stmt in reload_fn.body if isinstance(stmt, ast.Global) for name in stmt.names
    }
    reload_assigns = _assigned_names(reload_fn.body)

    assert REQUIRED_NOTIFY_CONFIG <= top_level_assigns
    assert REQUIRED_NOTIFY_CONFIG <= reload_globals
    assert REQUIRED_NOTIFY_CONFIG <= reload_assigns


def test_notify_config_defaults_and_reload_env_overrides(monkeypatch):
    with monkeypatch.context() as env:
        env.delenv("TI_NOTIFY_WEBHOOK", raising=False)
        env.delenv("TI_NOTIFY_TIMEOUT", raising=False)
        config.reload()

        assert config.NOTIFY_WEBHOOK == ""
        assert config.NOTIFY_TIMEOUT == 10.0

        env.setenv("TI_NOTIFY_WEBHOOK", "  https://example.invalid/hook?secret=abc  ")
        env.setenv("TI_NOTIFY_TIMEOUT", "2.5")
        config.reload()

        assert config.NOTIFY_WEBHOOK == "https://example.invalid/hook?secret=abc"
        assert config.NOTIFY_TIMEOUT == 2.5

    config.reload()


def test_send_bg_without_webhook_is_noop(monkeypatch):
    with monkeypatch.context() as env:
        env.delenv("TI_NOTIFY_WEBHOOK", raising=False)
        env.delenv("TI_NOTIFY_TIMEOUT", raising=False)
        config.reload()

        class NoThread:
            def __init__(self, *_args, **_kwargs):
                pytest.fail("send_bg must not start a thread without webhook")

        monkeypatch.setattr(notify.threading, "Thread", NoThread)
        monkeypatch.setattr(
            notify.urllib.request,
            "urlopen",
            lambda *_args, **_kwargs: pytest.fail("send_bg must not hit network without webhook"),
        )

        notify.send_bg("noop", "no webhook", task_id=1)

    config.reload()


def test_send_bg_posts_json_in_background(monkeypatch, caplog):
    webhook = "https://hooks.example.test/notify"
    threads: list[object] = []
    calls: list[dict[str, object]] = []

    class FakeThread:
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

    def fake_urlopen(request, timeout):
        calls.append(
            {
                "url": request.get_full_url(),
                "timeout": timeout,
                "body": json.loads(request.data.decode("utf-8")),
            }
        )

        class _Response:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        return _Response()

    with monkeypatch.context() as env:
        env.setenv("TI_NOTIFY_WEBHOOK", webhook)
        env.setenv("TI_NOTIFY_TIMEOUT", "3.5")
        config.reload()
        caplog.set_level(logging.INFO, logger="ti.notify")
        monkeypatch.setattr(notify.threading, "Thread", FakeThread)
        monkeypatch.setattr(notify.urllib.request, "urlopen", fake_urlopen)

        notify.send_bg("daily_pr_budget_pause", "reached", task_id=7, budget=2)

    assert len(threads) == 1
    assert threads[0].daemon is True
    assert threads[0].started is True
    assert calls == [
        {
            "url": webhook,
            "timeout": 3.5,
            "body": {
                "event": "daily_pr_budget_pause",
                "message": "reached",
                "payload": {"task_id": 7, "budget": 2},
            },
        }
    ]
    assert webhook not in caplog.text

    config.reload()


def test_send_bg_swallows_webhook_errors(monkeypatch):
    threads: list[object] = []

    class FakeThread:
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

    def fake_urlopen(*_args, **_kwargs):
        raise OSError("boom")

    with monkeypatch.context() as env:
        env.setenv("TI_NOTIFY_WEBHOOK", "https://hooks.example.test/notify")
        env.setenv("TI_NOTIFY_TIMEOUT", "1.0")
        config.reload()
        monkeypatch.setattr(notify.threading, "Thread", FakeThread)
        monkeypatch.setattr(notify.urllib.request, "urlopen", fake_urlopen)

        notify.send_bg("event", "message", extra="value")

    assert len(threads) == 1
    assert threads[0].started is True

    config.reload()
