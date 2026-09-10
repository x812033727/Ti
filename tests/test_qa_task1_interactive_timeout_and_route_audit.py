"""QA guard for task #1: interactive timeout wiring and core-route audit events."""

from __future__ import annotations

import pytest

from studio import config, events, ws


@pytest.fixture(autouse=True)
def _reload_config_after_test():
    yield
    config.reload()


def test_interactive_timeout_reload_drives_ws_helper(monkeypatch):
    """Env reload must affect the WS helper; default/off and improve mode stay unbounded."""
    monkeypatch.setenv("TI_INTERACTIVE_SESSION_TIMEOUT", "42.5")
    config.reload()

    assert config.INTERACTIVE_SESSION_TIMEOUT == 42.5
    assert ws._interactive_session_time_budget(improve_mode=False) == 42.5
    assert ws._interactive_session_time_budget(improve_mode=True) is None

    monkeypatch.setenv("TI_INTERACTIVE_SESSION_TIMEOUT", "0")
    config.reload()

    assert ws._interactive_session_time_budget(improve_mode=False) is None


def test_interactive_timeout_negative_is_treated_as_disabled(monkeypatch):
    """Bad/negative budgets must not shorten every interactive session immediately."""
    monkeypatch.setenv("TI_INTERACTIVE_SESSION_TIMEOUT", "-3")
    config.reload()

    assert config.INTERACTIVE_SESSION_TIMEOUT == -3
    assert ws._interactive_session_time_budget(improve_mode=False) is None


def test_core_change_route_skipped_count_never_negative():
    """Route accounting remains renderable even if a caller reports an impossible over-route."""
    ev = events.core_change_route(
        "qa-sid",
        source="qa_probe",
        parsed_count=1,
        routed_count=2,
        core_repo="x812033727/Ti",
    )

    assert ev.type == events.EventType.CORE_CHANGE_ROUTE
    assert ev.payload["parsed_count"] == 1
    assert ev.payload["routed_count"] == 2
    assert ev.payload["skipped_count"] == 0
