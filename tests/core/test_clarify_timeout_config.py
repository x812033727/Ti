from __future__ import annotations

import importlib
import logging
import os

import pytest

from studio import config

ENV = "TI_CLARIFY_TIMEOUT"


@pytest.fixture(autouse=True)
def _restore_config():
    original = os.environ.get(ENV)
    yield
    if original is None:
        os.environ.pop(ENV, None)
    else:
        os.environ[ENV] = original
    config.reload()


def test_clarify_timeout_bad_value_falls_back_with_warning(monkeypatch, caplog):
    monkeypatch.setenv(ENV, "abc")
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="studio.config"):
        config.reload()

    assert config.CLARIFY_TIMEOUT == 180.0
    messages = [record.getMessage() for record in caplog.records]
    assert any(ENV in message and "abc" in message and "180.0" in message for message in messages)


def test_clarify_timeout_empty_value_falls_back(monkeypatch):
    monkeypatch.setenv(ENV, "")
    importlib.reload(config)

    assert config.CLARIFY_TIMEOUT == 180.0


def test_clarify_timeout_decimal_value_is_accepted(monkeypatch):
    monkeypatch.setenv(ENV, "90.5")
    importlib.reload(config)

    assert config.CLARIFY_TIMEOUT == 90.5


def test_clarify_timeout_reload_syncs_env(monkeypatch):
    monkeypatch.setenv(ENV, "12.75")
    config.reload()

    assert config.CLARIFY_TIMEOUT == 12.75
