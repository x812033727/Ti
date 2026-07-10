"""QA 驗收：任務 #3「共用知識庫 NOTES.md」驗收標準專測。

驗收標準：
1. session 結束後 workspace 內「實體存在」NOTES.md。
2. 內容包含本場討論寫入的坑/決策（huddle 結論、critic 退回理由、任務摘要）。
3. 後續任務的 prompt 能讀回該檔內容。
4. 有測試驗證讀寫。
補充：路徑穿越防護、append 累積、預設關閉、不污染交付清單/zip。
"""

from __future__ import annotations

import pytest

from studio import config, events, workspace
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


def collect():
    bucket: list[events.StudioEvent] = []

    async def broadcast(ev: events.StudioEvent) -> None:
        bucket.append(ev)

    return bucket, broadcast


@pytest.fixture(autouse=True)
def _no_debate(monkeypatch):
    monkeypatch.setattr(config, "DEBATE_ROUNDS", 0)


def _experts(pm, eng, qa, senior):
    return {
        "pm": StubExpert(BY_KEY["pm"], pm),
        "engineer": StubExpert(BY_KEY["engineer"], eng),
        "qa": StubExpert(BY_KEY["qa"], qa),
        "senior": StubExpert(BY_KEY["senior"], senior),
    }


def _notes_workflow():
    return {
        "name": "notes-test",
        "description": "minimal notes integration flow",
        "stages": [
            {"type": "decompose"},
            {
                "type": "build",
                "task_pipeline": [
                    {"type": "implement", "assignee": "engineer"},
                    {
                        "type": "review",
                        "mode": "parallel",
                        "gate": [
                            {"role": "qa", "verdict": "qa_passed"},
                            {"role": "senior", "verdict": "senior_approved"},
                        ],
                    },
                    {
                        "type": "gate",
                        "roles": ["pm"],
                        "gate": [{"role": "pm", "verdict": "critic_blocks"}],
                    },
                ],
            },
        ],
    }


# === workspace 層：讀寫純函式 ==========================================


def test_append_creates_file_and_read_back(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "WORKSPACE_ROOT", tmp_path)
    sid = "rw"
    assert workspace.read_notes(sid) == ""  # 尚未寫入 → 空
    workspace.append_note(sid, "坑：浮點誤差需用 isclose 比較")
    workspace.append_note(sid, "決策：CLI 與核心邏輯分檔")

    # 實體檔案存在
    notes_path = workspace.workspace_path(sid) / "NOTES.md"
    assert notes_path.is_file()
    notes = workspace.read_notes(sid)
    assert "浮點誤差" in notes and "CLI 與核心邏輯分檔" in notes
    # append 是累積非覆寫
    assert notes.count("決策") == 1


def test_append_ignores_blank(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "WORKSPACE_ROOT", tmp_path)
    sid = "blank"
    workspace.append_note(sid, "   \n  ")
    workspace.append_note(sid, "")
    assert workspace.read_notes(sid) == ""


def test_notes_excluded_from_deliverables(tmp_path, monkeypatch):
    """NOTES.md 不算交付物：不進 list_files，也不進 zip。"""
    monkeypatch.setattr(config, "WORKSPACE_ROOT", tmp_path)
    sid = "deliv"
    root = workspace.create_workspace(sid)
    (root / "main.py").write_text("print(1)", encoding="utf-8")
    workspace.append_note(sid, "跨任務知識")

    assert "NOTES.md" not in workspace.list_files(sid)
    assert "main.py" in workspace.list_files(sid)
    import io
    import zipfile

    data = workspace.zip_workspace(sid)
    names = zipfile.ZipFile(io.BytesIO(data)).namelist()
    assert "NOTES.md" not in names
    assert "main.py" in names


def test_read_notes_path_traversal_safe(tmp_path, monkeypatch):
    """惡意 session_id 不會讀到 workspace 外的檔案。"""
    monkeypatch.setattr(config, "WORKSPACE_ROOT", tmp_path)
    (tmp_path / "secret.md").write_text("機密", encoding="utf-8")
    # 含路徑穿越字元的 id 被 workspace_path 清洗，落不到 secret
    notes = workspace.read_notes("../secret")
    assert "機密" not in notes


# === orchestrator 整合：寫入＋跨任務讀回 ================================


@pytest.mark.asyncio
async def test_session_writes_notes_and_reads_back(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "NOTES_ENABLED", True)
    # 驗證循序模式下後續任務讀回前一任務 NOTES；並行同波任務並發、波末才 flush，故釘循序。
    monkeypatch.setattr(config, "PARALLEL_TASKS_ENABLED", False)
    monkeypatch.setattr(config, "ENABLE_GIT", False)
    monkeypatch.setattr(config, "WORKSPACE_ROOT", tmp_path)
    sid = "flow"
    workspace.create_workspace(sid)

    bucket, broadcast = collect()
    experts = _experts(
        pm=["任務: 建立解析器\n任務: 建立輸出層", "決議: 完成", "檢討"],
        eng=["做好了"],
        qa=["驗證: PASS"],
        senior=["決議: 核可"],
    )
    session = StudioSession(
        sid,
        broadcast,
        experts=experts,
        cwd=workspace.workspace_path(sid),
        workflow=_notes_workflow(),
    )
    await session.run("需求")

    # 1) 結束後 NOTES.md 實體存在
    assert (workspace.workspace_path(sid) / "NOTES.md").is_file()
    # 2) 內容含本場任務摘要
    notes = workspace.read_notes(sid)
    assert "任務 #1 完成" in notes and "任務 #2 完成" in notes
    # 3) 第二任務 prompt 能讀回第一任務寫入的內容
    second_prompt = experts["engineer"].prompts[1]
    assert "團隊共用知識庫" in second_prompt
    assert "建立解析器" in second_prompt
    # 4) 不污染交付清單
    assert "NOTES.md" not in workspace.list_files(sid)


@pytest.mark.asyncio
async def test_huddle_conclusion_written_to_notes(tmp_path, monkeypatch):
    """卡關討論結論寫進 NOTES（坑/決策來自討論）。"""
    monkeypatch.setattr(config, "NOTES_ENABLED", True)
    monkeypatch.setattr(config, "HUDDLE_ENABLED", True)
    monkeypatch.setattr(config, "REFLEXION_ENABLED", False)  # 失敗輪不另蒸餾，保 hermetic
    monkeypatch.setattr(config, "ENABLE_GIT", False)
    monkeypatch.setattr(config, "WORKSPACE_ROOT", tmp_path)
    sid = "huddlenotes"
    workspace.create_workspace(sid)

    bucket, broadcast = collect()
    experts = _experts(
        pm=["任務: 實作", "決議: 未完成", "檢討"],
        eng=["v1", "v2", "v3", "改用替代方案 X 突破"],
        qa=["驗證: FAIL"],
        senior=["決議: 退回"],
    )
    session = StudioSession(
        sid,
        broadcast,
        experts=experts,
        cwd=workspace.workspace_path(sid),
        workflow=_notes_workflow(),
    )
    await session.run("需求")

    notes = workspace.read_notes(sid)
    assert "卡關討論" in notes  # huddle 結論已落地知識庫
    assert "已知限制" in notes  # 仍失敗的任務以已知限制記錄


@pytest.mark.asyncio
async def test_critic_rejection_written_to_notes(tmp_path, monkeypatch):
    """critic 退回理由寫進 NOTES（決策/坑來自異議）。"""
    monkeypatch.setattr(config, "NOTES_ENABLED", True)
    monkeypatch.setattr(config, "CRITIC_ENABLED", True)
    monkeypatch.setattr(config, "REFLEXION_ENABLED", False)  # 失敗輪不另蒸餾，保 hermetic
    monkeypatch.setattr(config, "ENABLE_GIT", False)
    monkeypatch.setattr(config, "WORKSPACE_ROOT", tmp_path)
    sid = "criticnotes"
    workspace.create_workspace(sid)

    bucket, broadcast = collect()
    experts = _experts(
        pm=["任務: 實作", "決議: 完成", "檢討"],
        eng=["做好了", "依異議修正"],
        qa=["驗證: PASS"],
        senior=["決議: 核可"],
    )
    critics = {"pm": StubExpert(BY_KEY["pm"], ["異議: 成立，缺少錯誤碼定義", "異議: 不成立"])}
    session = StudioSession(
        sid,
        broadcast,
        experts=experts,
        cwd=workspace.workspace_path(sid),
        critics=critics,
        workflow=_notes_workflow(),
    )
    await session.run("需求")

    notes = workspace.read_notes(sid)
    assert "異議退回" in notes
    assert "缺少錯誤碼定義" in notes


@pytest.mark.asyncio
async def test_notes_off_when_disabled(tmp_path, monkeypatch):
    """關閉 NOTES（預設已開，此處明確 pin 關）：不寫檔、不注入。"""
    monkeypatch.setattr(config, "NOTES_ENABLED", False)
    monkeypatch.setattr(config, "ENABLE_GIT", False)
    monkeypatch.setattr(config, "WORKSPACE_ROOT", tmp_path)
    sid = "off"
    workspace.create_workspace(sid)

    bucket, broadcast = collect()
    experts = _experts(
        pm=["任務: 建立 A", "決議: 完成", "檢討"],
        eng=["做好了"],
        qa=["驗證: PASS"],
        senior=["決議: 核可"],
    )
    session = StudioSession(sid, broadcast, experts=experts, cwd=workspace.workspace_path(sid))
    await session.run("需求")

    assert workspace.read_notes(sid) == ""
    assert not (workspace.workspace_path(sid) / "NOTES.md").exists()
    assert "團隊共用知識庫" not in experts["engineer"].prompts[0]


def test_notes_context_truncates_to_tail(tmp_path, monkeypatch):
    """NOTES 注入只取尾段 NOTES_MAX_CHARS 字（從段落邊界起）：防專案長跑 context 膨脹。"""
    from studio.orchestrator import LaneContext

    monkeypatch.setattr(config, "NOTES_ENABLED", True)
    monkeypatch.setattr(config, "NOTES_MAX_CHARS", 120)
    monkeypatch.setattr(config, "WORKSPACE_ROOT", tmp_path)
    sid = "notescap"
    workspace.create_workspace(sid)
    workspace.append_note(sid, "舊段" * 200)  # 遠超上限的舊知識
    workspace.append_note(sid, "新段重點：金額用整數分")

    async def bc(ev):
        pass

    session = StudioSession(sid, bc, experts={}, cwd=workspace.workspace_path(sid))
    ctx = LaneContext("main", workspace.workspace_path(sid), {})
    text = session._notes_context(ctx)
    assert "新段重點：金額用整數分" in text  # 最新知識保留
    assert "舊段舊段舊段" not in text  # 超限舊段被截掉
    # 上限為 0 時不截斷（停用治理）
    monkeypatch.setattr(config, "NOTES_MAX_CHARS", 0)
    assert "舊段舊段舊段" in session._notes_context(ctx)
