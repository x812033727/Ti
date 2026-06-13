"""持續改良「找問題」多視角：角色解析、並行提案、輪替合併去重、單視角還原、降級。"""

from __future__ import annotations

import pytest

from studio import config, projects, providers
from studio.improver import ProjectImprover
from studio.roles import Role


class _StubExpert:
    def __init__(self, role: Role, text: str):
        self.role = role
        self._text = text
        self.stopped = False

    async def speak(self, prompt: str, broadcast) -> str:
        self.prompt = prompt
        return self._text

    async def stop(self) -> None:
        self.stopped = True


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PROJECTS_ROOT", tmp_path / "projects")
    monkeypatch.setattr(config, "WORKSPACE_ROOT", tmp_path / "ws")
    monkeypatch.setattr(config, "OFFLINE_MODE", False)
    monkeypatch.setattr(config, "KNOWLEDGE_ENABLED", True)
    monkeypatch.setattr(config, "AUTOPILOT_EVAL_MEMORY", 0)  # 無成績單前綴，聚焦本測試


def _improver():
    project = projects.create("測試產品", vision="最好用的小工具")

    async def bc(ev):
        pass

    return ProjectImprover(project, bc), project


_SCRIPTS = {
    "senior": "任務: [P0/bug] 補上錯誤處理\n任務: 重構資料層",
    "pm": "任務: 加上匯出功能\n任務: 補上錯誤處理",  # 與 senior 重複一條
    "researcher": "重點: 同類產品都有快捷鍵\n任務: [P2/feature] 支援鍵盤快捷鍵",
}


def _patch_experts(monkeypatch, created: dict):
    def fake_make_expert(role: Role, session_id: str, cwd):
        ex = _StubExpert(role, _SCRIPTS.get(role.key, "任務: 通用建議"))
        created[role.key] = ex
        return ex

    monkeypatch.setattr(providers, "make_expert", fake_make_expert)


@pytest.mark.asyncio
async def test_three_views_merge_round_robin_and_dedupe(monkeypatch):
    monkeypatch.setattr(config, "DISCOVER_ROLES", ["senior", "pm", "researcher"])
    created: dict = {}
    _patch_experts(monkeypatch, created)
    imp, project = _improver()

    items = await imp._discover_with_experts(project["id"], "sid1")
    titles = [t["title"] for t in items]

    # 三視角各建一次、用後都 stop
    assert set(created) == {"senior", "pm", "researcher"}
    assert all(ex.stopped for ex in created.values())
    # 輪替合併：每視角第一條先進；依標題去重（「補上錯誤處理」只留一條）
    assert titles[:3] == ["補上錯誤處理", "加上匯出功能", "支援鍵盤快捷鍵"]
    assert titles.count("補上錯誤處理") == 1
    assert "重構資料層" in titles
    # 結構化標籤（#95 格式）被保留：P0/bug 與 P2/feature；未標籤視為 P1
    by_title = {t["title"]: t for t in items}
    assert by_title["補上錯誤處理"]["priority"] == 0
    assert by_title["補上錯誤處理"]["type"] == "bug"
    assert by_title["支援鍵盤快捷鍵"]["priority"] == 2
    assert by_title["加上匯出功能"]["priority"] == 1
    # 研究員產出沉澱 docs/RESEARCH.md（同知識沉澱管道）
    from studio import workspace

    研究 = workspace.read_doc_tail(projects.workspace_id(project["id"]), "RESEARCH.md", 4000)
    assert "同類產品都有快捷鍵" in 研究
    # 各視角 prompt 帶各自素材：pm 帶願景、researcher 被要求上網
    assert "最好用的小工具" in created["pm"].prompt
    assert "上網" in created["researcher"].prompt


@pytest.mark.asyncio
async def test_single_role_restores_old_behavior(monkeypatch):
    monkeypatch.setattr(config, "DISCOVER_ROLES", ["senior"])
    created: dict = {}
    _patch_experts(monkeypatch, created)
    imp, project = _improver()

    items = await imp._discover_with_experts(project["id"], "sid2")
    assert set(created) == {"senior"}
    assert [t["title"] for t in items] == ["補上錯誤處理", "重構資料層"]


@pytest.mark.asyncio
async def test_optional_role_degrades_when_disabled(monkeypatch):
    """researcher 不在 OPTIONAL_ROLES（被關）時自動跳過；未知鍵被過濾。"""
    monkeypatch.setattr(config, "DISCOVER_ROLES", ["senior", "researcher", "nosuch"])
    monkeypatch.setattr(config, "OPTIONAL_ROLES", {"architect"})  # researcher 被關
    created: dict = {}
    _patch_experts(monkeypatch, created)
    imp, project = _improver()

    await imp._discover_with_experts(project["id"], "sid3")
    assert set(created) == {"senior"}


@pytest.mark.asyncio
async def test_all_filtered_falls_back_to_senior(monkeypatch):
    monkeypatch.setattr(config, "DISCOVER_ROLES", ["nosuch"])
    created: dict = {}
    _patch_experts(monkeypatch, created)
    imp, project = _improver()

    items = await imp._discover_with_experts(project["id"], "sid4")
    assert set(created) == {"senior"}
    assert items  # 保底單視角仍有產出


@pytest.mark.asyncio
async def test_one_view_failure_does_not_break_others(monkeypatch):
    monkeypatch.setattr(config, "DISCOVER_ROLES", ["senior", "pm"])
    created: dict = {}

    class _Boom(_StubExpert):
        async def speak(self, prompt, broadcast):
            raise RuntimeError("api down")

    def fake_make_expert(role: Role, session_id: str, cwd):
        ex = (
            _Boom(role, "")
            if role.key == "senior"
            else _StubExpert(role, _SCRIPTS.get(role.key, ""))
        )
        created[role.key] = ex
        return ex

    monkeypatch.setattr(providers, "make_expert", fake_make_expert)
    imp, project = _improver()

    items = await imp._discover_with_experts(project["id"], "sid5")
    assert "加上匯出功能" in [t["title"] for t in items]  # pm 視角不受 senior 失敗影響
    assert all(ex.stopped for ex in created.values())  # 失敗者也被 stop


@pytest.mark.asyncio
async def test_discovery_routes_core_changes_to_core_backlog(tmp_path, monkeypatch):
    """找問題辨識出的 `核心改動:` 與專案任務分流：進核心 backlog（source=core），不進專案 backlog。"""
    from studio import backlog

    monkeypatch.setattr(config, "AUTOPILOT_STATE_DIR", tmp_path / "core")
    monkeypatch.setattr(config, "DISCOVER_ROLES", ["senior", "pm"])

    scripts = {
        "senior": "任務: 補上錯誤處理\n核心改動: [P0/bug] orchestrator 應支援核心同步發佈",
        "pm": "任務: 加上匯出功能",
    }

    def fake_make_expert(role: Role, session_id: str, cwd):
        return _StubExpert(role, scripts.get(role.key, "任務: 通用建議"))

    monkeypatch.setattr(providers, "make_expert", fake_make_expert)
    imp, project = _improver()

    items = await imp._discover_with_experts(project["id"], "sidc")
    titles = [t["title"] for t in items]

    # 核心改動不混進專案任務提案。
    assert "orchestrator 應支援核心同步發佈" not in titles
    assert "補上錯誤處理" in titles and "加上匯出功能" in titles
    # 核心改動路由到核心 backlog（預設 state_dir＝AUTOPILOT_STATE_DIR），標記 source="core"。
    core_tasks = backlog.list_tasks()
    assert [t["title"] for t in core_tasks] == ["orchestrator 應支援核心同步發佈"]
    assert all(t["source"] == "core" for t in core_tasks)
