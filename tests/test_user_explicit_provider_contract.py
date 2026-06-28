from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager

from studio import config
from studio.orchestrator import StudioSession
from studio.roles import BY_KEY, Role


class StubExpert:
    def __init__(self, role: Role, provider: str = "claude"):
        self.role = role
        self.provider = provider


@contextmanager
def _role_provider_env(**values: str) -> Iterator[None]:
    keys = ["TI_PROVIDER", *(f"TI_PROVIDER_{key.upper()}" for key in config.ROLE_KEYS)]
    old = {key: os.environ.get(key) for key in keys}
    try:
        for key in keys:
            os.environ.pop(key, None)
        os.environ["TI_PROVIDER"] = "claude"
        for key, value in values.items():
            os.environ[f"TI_PROVIDER_{key.upper()}"] = value
        config.reload()
        yield
    finally:
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        config.reload()


def _entry(key: str, *, ready: bool, used: float | None = None):
    rate_limits = None
    if used is not None:
        rate_limits = {"five_hour": {"used_percentage": used}, "error": None}
    return {"key": key, "ready": ready, "rate_limits": rate_limits}


def _all_constrained_snap():
    return {
        "ok": True,
        "updated_at": 1000.0,
        "providers": [
            _entry("claude", ready=True, used=95),
            _entry("codex", ready=True, used=99),
            _entry("minimax", ready=False),
            _entry("antigravity", ready=False),
        ],
    }


def _session(tmp_path, experts: dict[str, StubExpert] | None = None) -> StudioSession:
    async def broadcast(_ev):
        return None

    return StudioSession("explicit-provider-contract", broadcast, experts=experts, cwd=tmp_path)


def test_is_user_explicit_provider_uses_role_provider_whitelist() -> None:
    with _role_provider_env(engineer="codex"):
        assert config.role_provider("engineer") == "codex"
        assert config.is_user_explicit_provider("engineer") is True
        assert config.role_provider("pm") == ""
        assert config.is_user_explicit_provider("pm") is False


def test_is_user_explicit_provider_rejects_non_whitelisted_value() -> None:
    with _role_provider_env(engineer="BogusProvider"):
        assert config.role_provider("engineer") == ""
        assert config.is_user_explicit_provider("engineer") is False


def test_explicit_provider_overrides_keeps_same_dict_contract(tmp_path) -> None:
    experts = {
        "engineer": StubExpert(BY_KEY["engineer"], "codex"),
        "pm": StubExpert(BY_KEY["pm"], "claude"),
    }
    with _role_provider_env(engineer="codex"):
        session = _session(tmp_path, experts)

        assert session._explicit_provider_overrides(experts) == {"engineer": "codex"}


def test_pick_provider_explicit_override_wins_when_all_constrained(tmp_path) -> None:
    with _role_provider_env(engineer="codex"):
        session = _session(tmp_path)
        session._quota_snap = _all_constrained_snap()

        assert session._pick_provider(BY_KEY["engineer"], "claude") == "codex"
        assert "engineer" not in session._provider_constrained_pending
