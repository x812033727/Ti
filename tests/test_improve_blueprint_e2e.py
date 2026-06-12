"""產品藍圖 × 持續改良迴圈的離線端到端：生成→落盤→seed backlog→注入 requirement。

不需 API 金鑰（TI_OFFLINE 路徑用 OFFLINE_BLUEPRINT 常數）；以 stub session 聚焦驗證
藍圖機制本身（生成時機、優先級出列順序、context 注入、開關預設關閉時零影響）。
"""

from __future__ import annotations

import pytest

from studio import backlog, blueprint, config, improver, projects


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PROJECTS_ROOT", tmp_path / "projects")
    monkeypatch.setattr(config, "WORKSPACE_ROOT", tmp_path / "workspaces")
    monkeypatch.setattr(config, "HISTORY_ROOT", tmp_path / "hist")
    monkeypatch.setattr(config, "OFFLINE_MODE", True)
    monkeypatch.setattr(config, "OFFLINE_DELAY", 0.0)
    monkeypatch.setattr(config, "BLUEPRINT_ENABLED", True)
    return tmp_path


class _StubSession:
    """頂替 StudioSession：記錄 requirement、回報完成，不跑真討論（聚焦藍圖機制）。"""

    seen: list[str] = []

    def __init__(self, sid, broadcast, **kwargs):
        self.session_id = sid

    def request_stop(self):
        pass

    async def run(self, requirement: str) -> dict:
        _StubSession.seen.append(requirement)
        return {"completed": True, "followups": []}


async def _noop_broadcast(event):
    pass


async def test_blueprint_e2e_offline(env, monkeypatch):
    monkeypatch.setattr(improver, "StudioSession", _StubSession)
    _StubSession.seen = []
    proj = projects.create("示範產品", vision="輕鬆上手的示範")
    pid = proj["id"]

    imp = improver.ProjectImprover(proj, _noop_broadcast)
    await imp.run(max_cycles=1)

    # 藍圖雙落盤：機讀 json + 人讀 BLUEPRINT.md（在專案固定 workspace、進檔案面板）。
    data = blueprint.load(pid)
    assert data and len(data["features"]) == 3 and data["session_id"].startswith("pjbp")
    md = (projects.workspace_dir(pid) / "BLUEPRINT.md").read_text(encoding="utf-8")
    assert "核心功能" in md and "[P0]" in md

    # 功能餵進 backlog（source=blueprint、type=feature），且 P0 先出列被本輪消化。
    sdir = projects.state_dir(pid)
    tasks = backlog.list_tasks(state_dir=sdir)
    assert any(t["source"] == "blueprint" and t["type"] == "feature" for t in tasks)
    first_run = [t for t in tasks if t["status"] == "done"]
    assert first_run and first_run[0]["priority"] == 0  # P0「核心功能可運行」先做

    # requirement 注入藍圖區塊，且本輪任務正是 P0 功能。
    assert _StubSession.seen and "【產品藍圖" in _StubSession.seen[0]
    assert "核心功能可運行" in _StubSession.seen[0]


async def test_blueprint_generated_once(env, monkeypatch):
    monkeypatch.setattr(improver, "StudioSession", _StubSession)
    proj = projects.create("只生成一次")
    pid = proj["id"]
    imp = improver.ProjectImprover(proj, _noop_broadcast)
    await imp.run(max_cycles=1)
    first = blueprint.load(pid)["generated_at"]
    await imp.run(max_cycles=1)
    assert blueprint.load(pid)["generated_at"] == first  # exists() 守門，不重生成


async def test_blueprint_raw_fallback(env, monkeypatch):
    # PM 輸出完全不照格式 → 原文寫 BLUEPRINT.md、json 標記 raw、不餵 backlog、不擋迴圈。
    monkeypatch.setattr(improver, "OFFLINE_BLUEPRINT", "完全沒有照格式的自由發揮")
    proj = projects.create("降級專案")
    pid = proj["id"]
    imp = improver.ProjectImprover(proj, _noop_broadcast)
    await imp._ensure_blueprint()
    assert blueprint.load(pid)["raw"] is True
    md = (projects.workspace_dir(pid) / "BLUEPRINT.md").read_text(encoding="utf-8")
    assert "自由發揮" in md
    assert backlog.counts(state_dir=projects.state_dir(pid))["pending"] == 0
    assert blueprint.context(pid) == ""  # raw 藍圖不注入


async def test_blueprint_disabled_no_side_effects(env, monkeypatch):
    monkeypatch.setattr(config, "BLUEPRINT_ENABLED", False)
    monkeypatch.setattr(improver, "StudioSession", _StubSession)
    _StubSession.seen = []
    proj = projects.create("關閉開關", vision="維持現狀")
    pid = proj["id"]
    sdir = projects.state_dir(pid)
    backlog.add("既有任務", state_dir=sdir)

    imp = improver.ProjectImprover(proj, _noop_broadcast)
    await imp.run(max_cycles=1)

    assert not blueprint.exists(pid)
    assert not (projects.workspace_dir(pid) / "BLUEPRINT.md").exists()
    # requirement 與現行格式一致：無藍圖區塊。
    assert _StubSession.seen and "【產品藍圖" not in _StubSession.seen[0]
    assert "本輪改良任務：既有任務" in _StubSession.seen[0]
