"""Stage 4 專案部署探針：精確 revision、SSRF 防護與政策契約。"""

from __future__ import annotations

import socket

import pytest

from studio import autonomy, config, project_health


@pytest.fixture(autouse=True)
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(config, "PROJECTS_ROOT", tmp_path / "projects")


def _contract(**overrides):
    value = {
        "health_url": "https://service.example/healthz",
        "healthy_field": "ok",
        "revision_field": "build.git_sha",
        "timeout_s": 10,
        "poll_interval_s": 10,
    }
    value.update(overrides)
    return value


@pytest.mark.asyncio
async def test_probe_requires_health_and_exact_merge_revision(monkeypatch):
    sha = "a" * 40

    async def public(_host):
        return ["93.184.216.34"]

    async def good(*args, **kwargs):
        return True, {"ok": True, "build": {"git_sha": sha}}, "ok"

    monkeypatch.setattr(project_health, "_public_addresses", public)
    monkeypatch.setattr(project_health, "_once", good)
    assert await project_health.verify(_contract(), sha) == (
        True,
        "health_and_revision_verified",
    )

    async def stale(*args, **kwargs):
        return True, {"ok": True, "build": {"git_sha": "b" * 40}}, "ok"

    monkeypatch.setattr(project_health, "_once", stale)
    ok, detail = await project_health.verify(_contract(), sha)
    assert not ok and detail.endswith("healthy_revision_mismatch")


@pytest.mark.asyncio
async def test_private_or_mixed_dns_is_rejected_before_curl(monkeypatch):
    def private(*args, **kwargs):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443)),
        ]

    monkeypatch.setattr(project_health.socket, "getaddrinfo", private)
    assert await project_health._public_addresses("service.example") == []

    async def should_not_run(*args, **kwargs):
        raise AssertionError("private target must not reach curl")

    monkeypatch.setattr(project_health, "_once", should_not_run)
    ok, detail = await project_health.verify(_contract(), "a" * 40)
    assert not ok and detail == "health_host_not_public_or_unresolvable"


@pytest.mark.asyncio
async def test_curl_is_dns_pinned_https_bounded_and_no_redirect(monkeypatch):
    seen = []

    async def run(cmd, cwd=None, timeout=600):
        seen.append(cmd)
        return 0, '{"ok":true}\n200'

    monkeypatch.setattr(project_health.deploy, "_run", run)
    ok, body, detail = await project_health._once(
        "https://service.example/healthz", "service.example", "93.184.216.34", 10
    )
    assert ok and body == {"ok": True} and detail == "ok"
    cmd = seen[0]
    assert cmd[cmd.index("--proto") + 1] == "=https"
    assert cmd[cmd.index("--max-redirs") + 1] == "0"
    assert cmd[cmd.index("--noproxy") + 1] == "*"
    assert cmd[cmd.index("--resolve") + 1] == "service.example:443:93.184.216.34"
    assert "-L" not in cmd and "--location" not in cmd


@pytest.mark.parametrize(
    "url",
    [
        "http://service.example/healthz",
        "https://user:pass@service.example/healthz",
        "https://service.example:8443/healthz",
        "https://service.example/healthz?token=secret",
        "https://service.example/healthz#secret",
    ],
)
def test_policy_rejects_unsafe_health_urls(url):
    autonomy.ensure_policy("p1")
    with pytest.raises(autonomy.PolicyError, match="health_url"):
        autonomy.save_policy("p1", {"deployment": {"health_url": url}})


def test_external_stage4_contract_is_not_ready_without_revision_probe():
    autonomy.ensure_policy("p1")
    status = autonomy.deployment_contract_status("p1")
    assert not status["ready"] and "health_url_missing" in status["blocking_reasons"]
    autonomy.save_policy("p1", {"deployment": _contract()})
    assert autonomy.deployment_contract_status("p1")["ready"] is True
