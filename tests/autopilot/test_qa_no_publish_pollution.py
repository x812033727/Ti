"""QA guard: autopilot must never publish core changes through the project repo."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from studio import autopilot, config, publisher

_TASK = {"id": "91", "title": "repo guard", "detail": "keep core changes in autopilot repo"}
_AUTOPILOT_REPO = "core/autopilot"
_PROJECT_REPO = "product/project"


class RunSpy:
    def __init__(self, origin: str | None = None):
        self.calls: list[list[str]] = []
        self.origin = origin or f"https://github.com/{_AUTOPILOT_REPO}.git"

    async def __call__(self, cmd, cwd=None, timeout=600, **kwargs):
        self.calls.append(list(cmd))
        joined = " ".join(cmd)
        if "remote get-url --push origin" in joined:
            return (0, self.origin)
        if "rev-list --count" in joined:
            return (0, "1")
        if "pr view" in joined:
            return (0, "42")
        return (0, "")

    def joined(self) -> list[str]:
        return [" ".join(c) for c in self.calls]

    def called(self, fragment: str) -> bool:
        return any(fragment in j for j in self.joined())


@pytest.fixture(autouse=True)
def _forbid_real_subprocess(monkeypatch):
    async def _boom(*args, **kwargs):
        raise RuntimeError("test forbids spawning a real subprocess")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _boom)


@pytest.fixture(autouse=True)
def _base_config(monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_REPO", _AUTOPILOT_REPO)
    monkeypatch.setattr(config, "AUTOPILOT_BRANCH", "main")
    monkeypatch.setattr(config, "AUTOPILOT_DRYRUN", False)
    monkeypatch.setattr(config, "AUTOPILOT_FORCE_PUSH", False)
    monkeypatch.setattr(config, "AUTOPILOT_PROTECTION_CHECK", False)
    monkeypatch.setattr(config, "PUBLISH_REPO", "")
    # 本檔的黑白樣本聚焦 repo identity 污染防護；owner allowlist 放行本檔用的測試 owner，
    # allowlist 自身的黑樣本見下方 owner 案與 tests/publish/test_owner_allowlist.py。
    monkeypatch.setattr(config, "PUBLISH_OWNER_ALLOWLIST", frozenset({"core"}))


@pytest.mark.asyncio
async def test_merge_flow_observes_autopilot_repo_override(monkeypatch):
    spy = RunSpy()
    observed_repos: list[str] = []

    async def _merge_flow(number, payload, **kwargs):
        observed_repos.append(publisher.current_repo())
        return (publisher.MergeOutcome.MERGED, "merged")

    monkeypatch.setattr(autopilot, "_run", spy)
    monkeypatch.setattr(publisher, "_merge_flow", _merge_flow)

    ok, msg = await autopilot._commit_push_merge("/clone", _TASK)

    assert ok is True, msg
    assert observed_repos == [_AUTOPILOT_REPO]
    assert publisher.current_repo() == ""
    assert spy.called(f"pr create -R {_AUTOPILOT_REPO}")
    assert not spy.called(f"pr create -R {_PROJECT_REPO}")


@pytest.mark.asyncio
async def test_empty_autopilot_repo_aborts_before_push_or_pr(monkeypatch):
    run = AsyncMock()
    merge_flow = AsyncMock()
    monkeypatch.setattr(config, "AUTOPILOT_REPO", "")
    monkeypatch.setattr(autopilot, "_run", run)
    monkeypatch.setattr(publisher, "_merge_flow", merge_flow)

    ok, msg = await autopilot._commit_push_merge("/clone", _TASK)

    assert ok is False
    assert "AUTOPILOT_REPO" in msg
    run.assert_not_awaited()
    merge_flow.assert_not_awaited()


@pytest.mark.asyncio
async def test_publish_repo_different_from_autopilot_repo_aborts_without_side_effects(monkeypatch):
    run = AsyncMock()
    merge_flow = AsyncMock()
    monkeypatch.setattr(config, "PUBLISH_REPO", _PROJECT_REPO)
    monkeypatch.setattr(autopilot, "_run", run)
    monkeypatch.setattr(publisher, "_merge_flow", merge_flow)

    ok, msg = await autopilot._commit_push_merge("/clone", _TASK)

    assert ok is False
    assert "PUBLISH_REPO" in msg
    run.assert_not_awaited()
    merge_flow.assert_not_awaited()


@pytest.mark.asyncio
async def test_publish_repo_same_path_on_non_github_host_aborts_without_side_effects(monkeypatch):
    run = AsyncMock()
    merge_flow = AsyncMock()
    monkeypatch.setattr(config, "PUBLISH_REPO", f"https://evil.example/{_AUTOPILOT_REPO}.git")
    monkeypatch.setattr(autopilot, "_run", run)
    monkeypatch.setattr(publisher, "_merge_flow", merge_flow)

    ok, msg = await autopilot._commit_push_merge("/clone", _TASK)

    assert ok is False
    assert "PUBLISH_REPO" in msg
    run.assert_not_awaited()
    merge_flow.assert_not_awaited()


@pytest.mark.asyncio
async def test_publish_repo_same_as_autopilot_repo_does_not_block_autopilot_push(monkeypatch):
    spy = RunSpy()

    async def _merge_flow(number, payload, **kwargs):
        return (publisher.MergeOutcome.MERGED, "merged")

    monkeypatch.setattr(config, "PUBLISH_REPO", "Core/Autopilot")
    monkeypatch.setattr(autopilot, "_run", spy)
    monkeypatch.setattr(publisher, "_merge_flow", _merge_flow)

    ok, msg = await autopilot._commit_push_merge("/clone", _TASK)

    assert ok is True, msg
    assert spy.called(f"pr create -R {_AUTOPILOT_REPO}")
    assert any("push" in call for call in spy.calls)


@pytest.mark.asyncio
async def test_origin_push_url_must_match_autopilot_repo_before_push(monkeypatch):
    spy = RunSpy(origin=f"https://github.com/{_PROJECT_REPO}.git")
    merge_flow = AsyncMock()
    monkeypatch.setattr(autopilot, "_run", spy)
    monkeypatch.setattr(publisher, "_merge_flow", merge_flow)

    ok, msg = await autopilot._commit_push_merge("/clone", _TASK)

    assert ok is False
    assert "origin push URL" in msg
    assert not any("push" in call for call in spy.calls)
    merge_flow.assert_not_awaited()


@pytest.mark.asyncio
async def test_origin_push_url_same_path_on_non_github_host_aborts_before_push(monkeypatch):
    spy = RunSpy(origin=f"https://evil.example/{_AUTOPILOT_REPO}.git")
    merge_flow = AsyncMock()
    monkeypatch.setattr(autopilot, "_run", spy)
    monkeypatch.setattr(publisher, "_merge_flow", merge_flow)

    ok, msg = await autopilot._commit_push_merge("/clone", _TASK)

    assert ok is False
    assert "origin push URL" in msg
    assert not any("push" in call for call in spy.calls)
    merge_flow.assert_not_awaited()


# --- owner allowlist 案（發佈與建庫的 owner 護欄，黑白樣本）-------------------


@pytest.mark.asyncio
async def test_autopilot_repo_owner_not_in_allowlist_aborts_without_side_effects(monkeypatch):
    """AUTOPILOT_REPO 的 owner 不在 allowlist → 中止，無任何 git／merge 副作用（黑樣本）。"""
    run = AsyncMock()
    merge_flow = AsyncMock()
    monkeypatch.setattr(config, "PUBLISH_OWNER_ALLOWLIST", frozenset({"x812033727"}))
    monkeypatch.setattr(autopilot, "_run", run)
    monkeypatch.setattr(publisher, "_merge_flow", merge_flow)

    ok, msg = await autopilot._commit_push_merge("/clone", _TASK)

    assert ok is False
    assert "allowlist" in msg
    run.assert_not_awaited()
    merge_flow.assert_not_awaited()
    assert publisher.current_repo() == ""  # 覆寫未生效、未殘留


@pytest.mark.asyncio
async def test_autopilot_repo_owner_in_allowlist_proceeds(monkeypatch):
    """owner 在 allowlist（本檔 fixture 已放行 core）→ 照常 push＋開 PR（白樣本對照）。"""
    spy = RunSpy()

    async def _merge_flow(number, payload, **kwargs):
        return (publisher.MergeOutcome.MERGED, "merged")

    monkeypatch.setattr(autopilot, "_run", spy)
    monkeypatch.setattr(publisher, "_merge_flow", _merge_flow)

    ok, msg = await autopilot._commit_push_merge("/clone", _TASK)

    assert ok is True, msg
    assert any("push" in call for call in spy.joined())
