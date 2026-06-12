"""base_repo 接線：orchestrator prompt 注入 × improver 每輪同步工作基底。

沿用 tests/test_lessons_e2e.py 的 StubExpert 模式（不需 LLM / bwrap / 網路）：
- StudioSession 帶 base_repo 時，PM 拆解 prompt 必須告知「既有程式碼在工作目錄」；
  未帶時隻字不提；一次性 repo_url 路線文案不變。
- ProjectImprover 在藍圖生成前與每輪任務前各同步一次；fatal 時不開工。
"""

from __future__ import annotations

import pytest

from studio import config, events, improver, projects, repo_base, workspace
from studio.orchestrator import StudioSession
from studio.roles import BY_KEY, Role


class StubExpert:
    def __init__(self, role: Role, scripts: list[str]):
        self.role = role
        self._scripts = scripts
        self.calls = 0
        self.prompts: list[str] = []

    async def speak(self, prompt: str, broadcast) -> str:
        self.prompts.append(prompt)
        text = self._scripts[min(self.calls, len(self._scripts) - 1)]
        self.calls += 1
        await broadcast(
            events.expert_message("t", self.role.key, self.role.name, self.role.avatar, text)
        )
        return text

    async def stop(self) -> None:
        pass


async def _noop_broadcast(event):
    pass


def _experts():
    return {
        "pm": StubExpert(BY_KEY["pm"], ["任務: 做點事", "決議: 完成", "檢討：無"]),
        "engineer": StubExpert(BY_KEY["engineer"], ["做好了"]),
        "qa": StubExpert(BY_KEY["qa"], ["驗證: PASS"]),
        "senior": StubExpert(BY_KEY["senior"], ["決議: 核可"]),
    }


@pytest.fixture(autouse=True)
def _base_env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DEBATE_ROUNDS", 0)
    monkeypatch.setattr(config, "ENABLE_GIT", False)
    monkeypatch.setattr(config, "WORKSPACE_ROOT", tmp_path / "ws")
    monkeypatch.setattr(config, "PROJECTS_ROOT", tmp_path / "projects")
    monkeypatch.setattr(config, "HISTORY_ROOT", tmp_path / "hist")
    monkeypatch.setattr(config, "PUBLISH_BASE", "main")


async def _run(sid: str, **kwargs) -> dict:
    workspace.create_workspace(sid)
    experts = _experts()
    session = StudioSession(
        sid, _noop_broadcast, experts=experts, cwd=workspace.workspace_path(sid), **kwargs
    )
    await session.run("加個匯出功能")
    return experts


# --- orchestrator：PM 拆解 prompt 的 repo_note 三態 ----------------------


async def test_pm_prompt_mentions_base_repo(monkeypatch):
    experts = await _run("s-base", base_repo="me/product")
    decompose = experts["pm"].prompts[0]
    assert "me/product" in decompose
    assert "既有程式碼" in decompose
    assert "不要砍掉重練" in decompose
    assert "main 分支" in decompose  # 同步到的 base 分支要講清楚


async def test_pm_prompt_silent_without_base_repo():
    experts = await _run("s-plain")
    decompose = experts["pm"].prompts[0]
    assert "既有程式碼" not in decompose
    assert "不要砍掉重練" not in decompose


async def test_pm_prompt_repo_url_wording_unchanged():
    """一次性 repo_url 路線的既有文案不受 base_repo 功能影響。"""
    experts = await _run("s-url", repo_url="https://github.com/o/r")
    decompose = experts["pm"].prompts[0]
    assert "原始碼已 clone" in decompose
    assert "https://github.com/o/r" in decompose


# --- improver：藍圖前 + 每輪各同步一次；fatal 不開工 ----------------------


class _StubSession:
    seen: list[dict] = []

    def __init__(self, sid, broadcast, **kwargs):
        self.session_id = sid
        _StubSession.seen.append(kwargs)

    def request_stop(self):
        pass

    async def run(self, requirement: str) -> dict:
        return {"completed": True, "followups": []}


class EnsureSpy:
    def __init__(self, result: repo_base.SyncResult):
        self.result = result
        self.calls: list[dict] = []

    async def __call__(self, cwd, repo, *, broadcast=None, session_id=""):
        self.calls.append({"cwd": cwd, "repo": repo})
        return self.result


@pytest.fixture
def improve_env(monkeypatch):
    monkeypatch.setattr(config, "OFFLINE_MODE", True)
    monkeypatch.setattr(config, "OFFLINE_DELAY", 0.0)
    monkeypatch.setattr(config, "BLUEPRINT_ENABLED", True)
    monkeypatch.setattr(improver, "StudioSession", _StubSession)
    _StubSession.seen = []


async def test_improver_syncs_before_blueprint_and_each_cycle(improve_env, monkeypatch):
    spy = EnsureSpy(repo_base.SyncResult("cloned", "已以目標 repo 為基底"))
    monkeypatch.setattr(repo_base, "ensure_base", spy)
    proj = projects.create("改良中產品", vision="好用")
    projects.set_publish_repo(proj["id"], "me/product")
    proj = projects.get(proj["id"])

    imp = improver.ProjectImprover(proj, _noop_broadcast)
    summary = await imp.run(max_cycles=1)

    # 迴圈起跑（藍圖 commit 前）一次 ＋ 該輪任務前一次。
    assert len(spy.calls) == 2
    assert all(c["repo"] == "me/product" for c in spy.calls)
    assert summary["cycles"] == 1 and summary["done"] == 1
    # based → 該輪 session 收到 base_repo 旗標。
    assert _StubSession.seen and _StubSession.seen[0]["base_repo"] == "me/product"


async def test_improver_fatal_sync_stops_loop(improve_env, monkeypatch):
    spy = EnsureSpy(repo_base.SyncResult("error", "拿不到基底"))
    monkeypatch.setattr(repo_base, "ensure_base", spy)
    proj = projects.create("起不來", vision="x")
    projects.set_publish_repo(proj["id"], "me/product")
    proj = projects.get(proj["id"])

    imp = improver.ProjectImprover(proj, _noop_broadcast)
    summary = await imp.run(max_cycles=3)

    # 起跑同步 fatal：零輪、未動工、走標準 stopped 收尾。
    assert summary["cycles"] == 0 and summary["stopped"] is True
    assert _StubSession.seen == []
