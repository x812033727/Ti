from __future__ import annotations

import json

import pytest

from studio import config, events, flow
from studio.orchestrator import LaneContext, StudioSession
from studio.roles import BY_KEY, Role


class StubExpert:
    def __init__(self, role: Role, provider: str = "claude"):
        self.role = role
        self.provider = provider

    async def speak(self, prompt: str, broadcast):
        return "ok"

    async def stop(self) -> None:
        pass


def _entry(key: str, *, ready: bool, used: float | None = None, error: str | None = None):
    rate_limits = None
    if used is not None or error is not None:
        rate_limits = {"five_hour": {"used_percentage": used or 0}, "error": error}
    return {"key": key, "ready": ready, "rate_limits": rate_limits}


def _snap(*entries):
    return {"ok": True, "updated_at": 1000.0, "providers": list(entries)}


@pytest.fixture(autouse=True)
def _provider_defaults(monkeypatch):
    monkeypatch.setattr(config, "PROVIDER", "claude")
    monkeypatch.setattr(config, "ROLE_PROVIDERS", {k: "" for k in config.ROLE_KEYS})


def _session(tmp_path, monkeypatch, snap, experts):
    bucket: list[events.StudioEvent] = []

    async def broadcast(ev):
        bucket.append(ev)

    from studio import provider_quota, providers

    monkeypatch.setattr(provider_quota, "snapshot", lambda: snap)
    monkeypatch.setattr(
        providers,
        "make_expert",
        lambda role, session_id, cwd, *, provider=None: StubExpert(role, provider or "claude"),
    )
    s = StudioSession("s", broadcast, experts=experts, cwd=tmp_path)

    async def noop_workflow():
        return None

    monkeypatch.setattr(s, "_run_workflow", noop_workflow)
    return s, bucket


@pytest.mark.asyncio
async def test_preflight_rebinds_existing_member_to_least_constrained_provider(
    tmp_path, monkeypatch
):
    snap = _snap(
        _entry("claude", ready=True, used=95),
        _entry("minimax", ready=True, used=20),
        _entry("codex", ready=False),
        _entry("antigravity", ready=False),
    )
    experts = {"engineer": StubExpert(BY_KEY["engineer"], "claude")}
    s, _ = _session(tmp_path, monkeypatch, snap, experts)

    await s._run("req")

    assert s._experts["engineer"].provider == "minimax"
    assert s._recruit_providers["engineer"] == "minimax"


@pytest.mark.asyncio
async def test_preflight_rebind_is_preserved_in_production_lane_experts(tmp_path, monkeypatch):
    snap = _snap(
        _entry("claude", ready=True, used=95),
        _entry("minimax", ready=True, used=20),
        _entry("codex", ready=False),
        _entry("antigravity", ready=False),
    )
    experts = {"engineer": StubExpert(BY_KEY["engineer"], "claude")}
    s, _ = _session(tmp_path, monkeypatch, snap, experts)

    await s._run("req")
    lane_experts = s._build_lane_experts("lane-a", tmp_path / "lane-a")

    assert lane_experts["engineer"].provider == "minimax"


@pytest.mark.asyncio
async def test_explicit_role_provider_is_not_rebound_or_reported(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ROLE_PROVIDERS", {**config.ROLE_PROVIDERS, "engineer": "codex"})
    snap = _snap(
        _entry("claude", ready=True, used=95),
        _entry("codex", ready=True, used=99),
        _entry("minimax", ready=True, used=10),
        _entry("antigravity", ready=False),
    )
    experts = {"engineer": StubExpert(BY_KEY["engineer"], "codex")}
    s, bucket = _session(tmp_path, monkeypatch, snap, experts)

    assert s._pick_provider(BY_KEY["engineer"], "claude") == "codex"
    await s._run("req")

    assert s._experts["engineer"] is experts["engineer"]
    assert not [e for e in bucket if e.type == events.EventType.PROVIDER_CONSTRAINED]


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["openai", "gemini"])
async def test_explicit_openai_compatible_provider_is_not_rebound(
    tmp_path, monkeypatch, provider
):
    monkeypatch.setattr(config, "ROLE_PROVIDERS", {**config.ROLE_PROVIDERS, "engineer": provider})
    snap = _snap(
        _entry(provider, ready=True, used=99),
        _entry("minimax", ready=True, used=10),
        _entry("claude", ready=True, used=20),
    )
    experts = {"engineer": StubExpert(BY_KEY["engineer"], provider)}
    s, bucket = _session(tmp_path, monkeypatch, snap, experts)

    assert s._pick_provider(BY_KEY["engineer"], "minimax") == provider
    await s._run("req")

    assert s._experts["engineer"] is experts["engineer"]
    assert s._experts["engineer"].provider == provider
    assert not [e for e in bucket if e.type == events.EventType.PROVIDER_CONSTRAINED]


@pytest.mark.asyncio
async def test_preflight_all_constrained_emits_event_and_audit(tmp_path, monkeypatch):
    state_dir = tmp_path / "ap"
    monkeypatch.setattr(config, "AUTOPILOT_STATE_DIR", state_dir)
    snap = _snap(
        _entry("claude", ready=True, used=95),
        _entry("minimax", ready=False),
        _entry("codex", ready=False),
        _entry("antigravity", ready=False),
    )
    experts = {"engineer": StubExpert(BY_KEY["engineer"], "claude")}
    s, bucket = _session(tmp_path, monkeypatch, snap, experts)

    await s._run("req")

    evs = [e for e in bucket if e.type == events.EventType.PROVIDER_CONSTRAINED]
    assert len(evs) == 1
    assert evs[0].payload["role"] == "engineer"
    assert evs[0].payload["provider"] == "claude"
    assert evs[0].payload["reason"] == "no_provider_ready"
    rec = json.loads((state_dir / "audit.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert rec["event"] == "provider_constrained"
    assert rec["role"] == "engineer"
    assert rec["snapshot"]["claude"]["max_used"] == 95


@pytest.mark.asyncio
async def test_recruit_all_constrained_emits_event(tmp_path, monkeypatch):
    state_dir = tmp_path / "ap"
    monkeypatch.setattr(config, "AUTOPILOT_STATE_DIR", state_dir)
    snap = _snap(
        _entry("claude", ready=True, used=95),
        _entry("minimax", ready=False),
        _entry("codex", ready=False),
        _entry("antigravity", ready=False),
    )
    s, bucket = _session(tmp_path, monkeypatch, snap, {})
    s._quota_snap = snap
    s._recruit_factory = lambda role, cwd, provider: StubExpert(role, provider)
    ctx = LaneContext("main", tmp_path, {})

    await s._recruit(ctx, BY_KEY["architect"], "", "庫招募")

    assert ctx.experts["architect"].provider == "claude"
    assert [e for e in bucket if e.type == events.EventType.PROVIDER_CONSTRAINED]


def test_plan_preflight_rebind_rebinds_constrained_bindings():
    snap = _snap(
        _entry("claude", ready=True, used=95),
        _entry("minimax", ready=True, used=10),
        _entry("codex", ready=True, used=20),
        _entry("antigravity", ready=False),
    )
    assert flow.plan_preflight_rebind({"engineer": "claude"}, snap, {}) == [
        ("engineer", "claude", "minimax")
    ]


def test_plan_preflight_rebind_skips_explicit_overrides():
    snap = _snap(
        _entry("claude", ready=True, used=95),
        _entry("minimax", ready=True, used=10),
    )
    plan = flow.plan_preflight_rebind({"engineer": "claude"}, snap, {"engineer": "codex"})
    assert plan == []


def test_plan_preflight_rebind_skips_explicit_override_even_when_provider_constrained():
    snap = _snap(
        _entry("claude", ready=True, used=95),
        _entry("minimax", ready=True, used=10),
    )
    plan = flow.plan_preflight_rebind({"engineer": "claude"}, snap, {"engineer": "claude"})
    assert plan == []


def test_plan_preflight_rebind_all_constrained_has_no_plan():
    snap = _snap(
        _entry("claude", ready=True, used=95),
        _entry("minimax", ready=False),
    )
    assert flow.plan_preflight_rebind({"engineer": "claude"}, snap, {}) == []


def test_apply_preflight_rebind_uses_injected_factory(tmp_path):
    async def broadcast(_ev):
        pass

    calls = []

    def factory(role, session_id, cwd, *, provider=None):
        calls.append((role.key, session_id, cwd, provider))
        return StubExpert(role, provider or "claude")

    experts = {"engineer": StubExpert(BY_KEY["engineer"], "claude")}
    s = StudioSession("s", broadcast, experts=experts, cwd=tmp_path)

    s._apply_preflight_rebind([("engineer", "claude", "minimax")], expert_factory=factory)

    assert calls == [("engineer", "s", tmp_path, "minimax")]
    assert s._experts["engineer"].provider == "minimax"
    assert s._recruit_providers["engineer"] == "minimax"


@pytest.mark.asyncio
async def test_preflight_rebinds_all_members_on_constrained_provider(tmp_path, monkeypatch):
    snap = _snap(
        _entry("claude", ready=True, used=95),
        _entry("minimax", ready=True, used=10),
        _entry("codex", ready=True, used=20),
    )
    experts = {
        "engineer": StubExpert(BY_KEY["engineer"], "claude"),
        "qa": StubExpert(BY_KEY["qa"], "claude"),
        "pm": StubExpert(BY_KEY["pm"], "claude"),
    }
    s, _ = _session(tmp_path, monkeypatch, snap, experts)

    await s._run("req")

    assert {key: ex.provider for key, ex in s._experts.items()} == {
        "engineer": "minimax",
        "qa": "minimax",
        "pm": "minimax",
    }
    assert s._recruit_providers == {
        "engineer": "minimax",
        "qa": "minimax",
        "pm": "minimax",
    }


@pytest.mark.asyncio
async def test_handle_all_constrained_audit_io_failure_does_not_break_session(
    tmp_path, monkeypatch
):
    """白樣本（audit IO 容錯）：當 AUTOPILOT_STATE_DIR 寫入失敗時，session 不掛、不 raise、
    仍發出 provider_constrained 事件。

    用 mock 讓 path.open 拋 OSError，驗證 broadcast 仍走完、無例外冒出。
    """
    snap = _snap(
        _entry("claude", ready=True, used=95),
        _entry("minimax", ready=False),
        _entry("codex", ready=False),
        _entry("antigravity", ready=False),
    )
    state_dir = tmp_path / "ap"
    monkeypatch.setattr(config, "AUTOPILOT_STATE_DIR", state_dir)
    experts = {"engineer": StubExpert(BY_KEY["engineer"], "claude")}
    s, bucket = _session(tmp_path, monkeypatch, snap, experts)

    def _boom(*_a, **_k):
        raise OSError("simulated disk full")

    monkeypatch.setattr("pathlib.Path.open", _boom)

    await s._handle_all_constrained("engineer", "claude", snap)

    events_emit = [e for e in bucket if e.type == events.EventType.PROVIDER_CONSTRAINED]
    assert len(events_emit) == 1
    assert events_emit[0].payload["role"] == "engineer"
