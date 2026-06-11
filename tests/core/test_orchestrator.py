"""用 stub 專家測試工作流程狀態機，不呼叫真正的 LLM。"""

from __future__ import annotations

import asyncio

import pytest

from studio import config, events
from studio.orchestrator import (
    StudioSession,
    critic_blocks,
    is_stalled,
    parse_tasks,
    pm_done,
    qa_passed,
    senior_approved,
    text_similarity,
)
from studio.roles import BY_KEY, Role


class StubExpert:
    """依角色給腳本化回應，記錄被呼叫次數與收到的 prompt。"""

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


@pytest.fixture(autouse=True)
def _no_debate(monkeypatch):
    """預設關閉架構辯論，讓流程測試專注在逐任務迴圈。"""
    monkeypatch.setattr(config, "DEBATE_ROUNDS", 0)


def collect():
    bucket: list[events.StudioEvent] = []

    async def broadcast(ev: events.StudioEvent) -> None:
        bucket.append(ev)

    return bucket, broadcast


def types(bucket):
    return [e.type for e in bucket]


def _experts(pm, eng, qa, senior):
    return {
        "pm": StubExpert(BY_KEY["pm"], pm),
        "engineer": StubExpert(BY_KEY["engineer"], eng),
        "qa": StubExpert(BY_KEY["qa"], qa),
        "senior": StubExpert(BY_KEY["senior"], senior),
    }


# --- 決議解析 -----------------------------------------------------------


def test_qa_passed_parsing():
    assert qa_passed("跑了測試\n驗證: PASS")
    assert not qa_passed("有錯\n驗證：FAIL")
    assert qa_passed("一切正常")  # 後備：無明顯失敗字
    assert not qa_passed("test failed")  # 後備：偵測到失敗


def test_senior_parsing():
    assert senior_approved("看起來不錯\n決議: 核可")
    assert not senior_approved("有問題\n決議：退回")


def test_pm_done_parsing():
    assert pm_done("符合\n決議: 完成")
    assert not pm_done("還缺測試\n決議：未完成")


def test_parse_tasks_bullets():
    text = "任務清單:\n- 建立 CLI\n- 加入分類邏輯\n2. 寫說明"
    tasks = parse_tasks(text)
    assert "建立 CLI" in tasks
    assert "寫說明" in tasks
    assert parse_tasks("沒有條列") == ["實作需求"]


def test_parse_tasks_structured():
    text = "任務: 建立 CLI\n任務: 加入分類\n驗收標準: 能跑\n執行指令: python main.py"
    tasks = parse_tasks(text)
    assert tasks == ["建立 CLI", "加入分類"]  # 優先 `任務:`，不含驗收/執行指令


# --- 流程 ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_happy_path_one_task_one_round():
    bucket, broadcast = collect()
    experts = _experts(
        pm=["任務: 實作 BMI", "決議: 完成", "做得不錯，下次可加更多測試"],
        eng=["已建立 bmi.py"],
        qa=["測試全過\n驗證: PASS"],
        senior=["品質良好\n決議: 核可"],
    )
    session = StudioSession("t", broadcast, experts=experts, cwd=None)
    await session.run("做一個 BMI CLI")

    ts = types(bucket)
    assert events.EventType.SESSION_STARTED in ts
    assert events.EventType.DONE in ts
    done = [e for e in bucket if e.type == events.EventType.DONE][0]
    assert done.payload["completed"] is True
    assert experts["engineer"].calls == 1


@pytest.mark.asyncio
async def test_retry_then_pass():
    bucket, broadcast = collect()
    experts = _experts(
        pm=["任務: 實作", "決議: 完成", "檢討完畢"],
        eng=["第一版", "已修正"],
        qa=["有錯\n驗證: FAIL", "修好了\n驗證: PASS"],
        senior=["先不核可\n決議: 退回", "可以了\n決議: 核可"],
    )
    session = StudioSession("t", broadcast, experts=experts, cwd=None)
    await session.run("需求")

    # 第一輪失敗 → 工程師應被呼叫兩次（同一任務內改進）
    assert experts["engineer"].calls == 2
    results = [e for e in bucket if e.type == events.EventType.RUN_RESULT]
    assert results[0].payload["passed"] is False
    assert results[1].payload["passed"] is True
    # 第二輪工程師 prompt 應帶入回饋意見
    assert "高級工程師審查意見" in experts["engineer"].prompts[1]


@pytest.mark.asyncio
async def test_per_task_iteration_two_tasks():
    bucket, broadcast = collect()
    experts = _experts(
        pm=["任務: 建立 A\n任務: 建立 B", "決議: 完成", "檢討"],
        eng=["做好了"],
        qa=["驗證: PASS"],
        senior=["決議: 核可"],
    )
    session = StudioSession("t", broadcast, experts=experts, cwd=None)
    await session.run("需求")

    # 兩個任務各跑一輪
    assert experts["engineer"].calls == 2
    task_done = [
        e
        for e in bucket
        if e.type == events.EventType.TASK_STATUS and e.payload["status"] == "done"
    ]
    assert {e.payload["id"] for e in task_done} == {1, 2}
    done = [e for e in bucket if e.type == events.EventType.DONE][0]
    assert done.payload["completed"] is True


@pytest.mark.asyncio
async def test_debate_runs(monkeypatch):
    monkeypatch.setattr(config, "DEBATE_ROUNDS", 1)
    bucket, broadcast = collect()
    experts = _experts(
        pm=["任務: 實作", "決議: 完成", "檢討"],
        eng=["提案", "實作"],
        qa=["驗證: PASS"],
        senior=["點評", "決議: 核可"],
    )
    session = StudioSession("t", broadcast, experts=experts, cwd=None)
    await session.run("需求")

    phases = [e.payload["phase"] for e in bucket if e.type == events.EventType.PHASE_CHANGE]
    assert "架構討論" in phases
    # 辯論各多一次發言：工程師提案+實作=2、高級工程師點評+審查=2
    assert experts["engineer"].calls == 2
    assert experts["senior"].calls == 2


def test_critic_blocks_parsing():
    assert critic_blocks("看了一下\n異議: 成立")  # 標記成立 → 退回
    assert not critic_blocks("沒問題\n異議: 不成立")  # 標記不成立 → 放行
    assert not critic_blocks("一切都好")  # 後備：無反對字 → 放行
    assert critic_blocks("這還不算完成")  # 後備：明確反對 → 退回


@pytest.mark.asyncio
async def test_critic_blocks_then_passes(monkeypatch):
    """critic 第一次異議成立 → 退回再修；第二次不成立 → 放行（退回路徑）。"""
    monkeypatch.setattr(config, "CRITIC_ENABLED", True)
    bucket, broadcast = collect()
    experts = _experts(
        pm=["任務: 實作", "決議: 完成", "檢討"],
        eng=["第一版", "依異議修正"],
        qa=["驗證: PASS"],
        senior=["決議: 核可"],
    )
    critics = {"pm": StubExpert(BY_KEY["pm"], ["異議: 成立，缺錯誤處理", "異議: 不成立"])}
    session = StudioSession("t", broadcast, experts=experts, cwd=None, critics=critics)
    await session.run("需求")

    # qa/senior 都通過，但 critic 第一次退回 → 工程師被迫再改一輪。
    assert experts["engineer"].calls == 2
    assert critics["pm"].calls == 2
    reviews = [e for e in bucket if e.type == events.EventType.CRITIC_REVIEW]
    assert reviews[0].payload["passed"] is False
    assert reviews[-1].payload["passed"] is True
    assert "異議檢查" in experts["engineer"].prompts[1]
    done = [e for e in bucket if e.type == events.EventType.DONE][0]
    assert done.payload["completed"] is True


@pytest.mark.asyncio
async def test_critic_passes_first_time(monkeypatch):
    """critic 一次就放行（放行路徑），不增加工程師輪數。"""
    monkeypatch.setattr(config, "CRITIC_ENABLED", True)
    bucket, broadcast = collect()
    experts = _experts(
        pm=["任務: 實作", "決議: 完成", "檢討"],
        eng=["做好了"],
        qa=["驗證: PASS"],
        senior=["決議: 核可"],
    )
    critics = {
        "pm": StubExpert(BY_KEY["pm"], ["異議: 不成立"]),
        "senior": StubExpert(BY_KEY["senior"], ["異議: 不成立"]),
    }
    session = StudioSession("t", broadcast, experts=experts, cwd=None, critics=critics)
    await session.run("需求")

    assert experts["engineer"].calls == 1
    reviews = [e for e in bucket if e.type == events.EventType.CRITIC_REVIEW]
    assert reviews and all(e.payload["passed"] for e in reviews)
    done = [e for e in bucket if e.type == events.EventType.DONE][0]
    assert done.payload["completed"] is True


@pytest.mark.asyncio
async def test_critic_final_gate_blocks(monkeypatch):
    """最終驗收時 senior 視角 critic 異議成立 → 整體未完成。"""
    monkeypatch.setattr(config, "CRITIC_ENABLED", True)
    bucket, broadcast = collect()
    experts = _experts(
        pm=["任務: 實作", "決議: 完成", "檢討"],
        eng=["做好了"],
        qa=["驗證: PASS"],
        senior=["決議: 核可"],
    )
    critics = {
        "pm": StubExpert(BY_KEY["pm"], ["異議: 不成立"]),  # 任務 gate 放行
        "senior": StubExpert(BY_KEY["senior"], ["異議: 成立，整合未驗證"]),  # 最終 gate 退回
    }
    session = StudioSession("t", broadcast, experts=experts, cwd=None, critics=critics)
    await session.run("需求")

    done = [e for e in bucket if e.type == events.EventType.DONE][0]
    assert done.payload["completed"] is False


@pytest.mark.asyncio
async def test_critic_disabled_by_default():
    """預設不啟用 critic：無 CRITIC_REVIEW 事件、不影響既有流程。"""
    bucket, broadcast = collect()
    experts = _experts(
        pm=["任務: 實作", "決議: 完成", "檢討"],
        eng=["做好了"],
        qa=["驗證: PASS"],
        senior=["決議: 核可"],
    )
    critics = {"pm": StubExpert(BY_KEY["pm"], ["異議: 成立"])}
    session = StudioSession("t", broadcast, experts=experts, cwd=None, critics=critics)
    await session.run("需求")

    assert events.EventType.CRITIC_REVIEW not in types(bucket)
    assert experts["engineer"].calls == 1
    assert critics["pm"].calls == 0


@pytest.mark.asyncio
async def test_huddle_triggers_on_stall(monkeypatch):
    """任務跑滿輪數仍 FAIL → 觸發 huddle、給 1 輪重試、仍失敗標為已知限制。"""
    monkeypatch.setattr(config, "HUDDLE_ENABLED", True)
    bucket, broadcast = collect()
    experts = _experts(
        pm=["任務: 實作", "決議: 未完成", "檢討"],
        eng=["第一版", "再試", "還是這樣", "huddle 後重試"],
        qa=["有錯\n驗證: FAIL"],  # 一律 FAIL（腳本用盡回最後一句）
        senior=["先不核可\n決議: 退回"],  # 一律退回
    )
    session = StudioSession("t", broadcast, experts=experts, cwd=None)
    await session.run("需求")

    huddles = [e for e in bucket if e.type == events.EventType.HUDDLE]
    # 至少有一次討論事件 + 一次「已知限制」事件
    assert any(not e.payload["limitation"] for e in huddles)
    assert any(e.payload["limitation"] for e in huddles)
    # huddle 後有重試 → 工程師被呼叫次數超過 TASK_MAX_ROUNDS（含 huddle 發言與重試）
    assert experts["engineer"].calls > config.TASK_MAX_ROUNDS
    # 任務最終維持 review（不消失於看板），且被標記限制
    assert session._tasks[0].get("limitation") is True
    task_review = [
        e
        for e in bucket
        if e.type == events.EventType.TASK_STATUS and e.payload["status"] == "review"
    ]
    assert task_review


@pytest.mark.asyncio
async def test_huddle_disabled_by_default():
    """預設不啟用 huddle：滿輪 FAIL 後直接收尾，無 HUDDLE 事件、無重試。"""
    bucket, broadcast = collect()
    experts = _experts(
        pm=["任務: 實作", "決議: 未完成", "檢討"],
        eng=["第一版"],
        qa=["有錯\n驗證: FAIL"],
        senior=["決議: 退回"],
    )
    session = StudioSession("t", broadcast, experts=experts, cwd=None)
    await session.run("需求")

    assert events.EventType.HUDDLE not in types(bucket)
    assert experts["engineer"].calls == config.TASK_MAX_ROUNDS


def test_stall_pure_functions():
    assert text_similarity("一樣的話", "一樣的話") == 1.0
    assert text_similarity("完全不同 ABC", "毫不相干 XYZ") < 0.5
    # 連續兩輪幾乎相同 → 停滯
    assert is_stalled(["改了 X", "還是 X 的內容相同", "還是 X 的內容相同"], rounds=2)
    # 有實質差異 → 不停滯
    assert not is_stalled(["第一版實作", "完全改寫的第二版"], rounds=2)
    # rounds<=1 或歷史不足 → 不判定
    assert not is_stalled(["a", "a"], rounds=1)
    assert not is_stalled(["a"], rounds=2)


@pytest.mark.asyncio
async def test_stall_breaks_early(tmp_path, monkeypatch):
    """連續兩輪只重述 → 提早收斂、發可觀察事件、不跑滿全部輪數。"""
    from studio import runner, workspace

    async def _noop_init(cwd):
        return True

    async def _noop_commit(cwd, message):
        return None  # 無檔案變動 → commit hash 不變

    monkeypatch.setattr(runner, "git_init", _noop_init)
    monkeypatch.setattr(runner, "git_commit", _noop_commit)
    monkeypatch.setattr(config, "ENABLE_GIT", True)
    monkeypatch.setattr(config, "STALL_ROUNDS", 2)
    monkeypatch.setattr(config, "TASK_MAX_ROUNDS", 5)
    monkeypatch.setattr(config, "WORKSPACE_ROOT", tmp_path)
    sid = "stallflow"
    workspace.create_workspace(sid)

    bucket, broadcast = collect()
    experts = _experts(
        pm=["任務: 實作", "決議: 未完成", "檢討"],
        eng=["我還是用同樣的做法，沒有改變"],  # 每輪重複同一句（腳本用盡回最後一句）
        qa=["有錯\n驗證: FAIL"],
        senior=["決議: 退回"],
    )
    session = StudioSession(sid, broadcast, experts=experts, cwd=workspace.workspace_path(sid))
    await session.run("需求")

    # 第 2 輪偵測到停滯即收斂，不會跑滿 5 輪
    assert experts["engineer"].calls == 2
    phases = [e.payload["phase"] for e in bucket if e.type == events.EventType.PHASE_CHANGE]
    assert "停滯收斂" in phases


@pytest.mark.asyncio
async def test_stall_disabled_with_no_cwd():
    """cwd=None（純單元測試情境）下不偵測停滯，照常跑滿輪數。"""
    bucket, broadcast = collect()
    experts = _experts(
        pm=["任務: 實作", "決議: 未完成", "檢討"],
        eng=["一樣的內容重複出現"],
        qa=["有錯\n驗證: FAIL"],
        senior=["決議: 退回"],
    )
    session = StudioSession("t", broadcast, experts=experts, cwd=None)
    await session.run("需求")
    # cwd=None → _stalled 一律 False → 跑滿 TASK_MAX_ROUNDS
    assert experts["engineer"].calls == config.TASK_MAX_ROUNDS


@pytest.mark.asyncio
async def test_stall_disabled_when_rounds_le_one(tmp_path, monkeypatch):
    """STALL_ROUNDS<=1 視為停用：即使連續重述也跑滿輪數、不提早收斂。"""
    from studio import runner, workspace

    async def _noop_init(cwd):
        return True

    async def _noop_commit(cwd, message):
        return None

    monkeypatch.setattr(runner, "git_init", _noop_init)
    monkeypatch.setattr(runner, "git_commit", _noop_commit)
    monkeypatch.setattr(config, "ENABLE_GIT", True)
    monkeypatch.setattr(config, "STALL_ROUNDS", 1)  # 關閉
    monkeypatch.setattr(config, "TASK_MAX_ROUNDS", 3)
    monkeypatch.setattr(config, "WORKSPACE_ROOT", tmp_path)
    sid = "nostall"
    workspace.create_workspace(sid)

    bucket, broadcast = collect()
    experts = _experts(
        pm=["任務: 實作", "決議: 未完成", "檢討"],
        eng=["重複內容重複內容"],
        qa=["有錯\n驗證: FAIL"],
        senior=["決議: 退回"],
    )
    session = StudioSession(sid, broadcast, experts=experts, cwd=workspace.workspace_path(sid))
    await session.run("需求")

    assert experts["engineer"].calls == 3  # 跑滿，未提早收斂
    phases = [e.payload["phase"] for e in bucket if e.type == events.EventType.PHASE_CHANGE]
    assert "停滯收斂" not in phases


@pytest.mark.asyncio
async def test_all_mechanisms_off_matches_baseline():
    """四機制全關（含非 offline）時，happy path 不產生任何新機制事件，行為同既有。"""
    # 直接驗證預設值已是關閉狀態（不靠 monkeypatch，確保預設相容）
    for name in ("HUDDLE_ENABLED", "CRITIC_ENABLED", "NOTES_ENABLED", "OFFLINE_MODE"):
        assert getattr(config, name) is False, f"{name} 預設應為 False"

    bucket, broadcast = collect()
    experts = _experts(
        pm=["任務: 實作", "決議: 完成", "檢討"],
        eng=["做好了"],
        qa=["驗證: PASS"],
        senior=["決議: 核可"],
    )
    session = StudioSession("t", broadcast, experts=experts, cwd=None)
    await session.run("需求")

    ts = types(bucket)
    assert events.EventType.HUDDLE not in ts
    assert events.EventType.CRITIC_REVIEW not in ts
    assert experts["engineer"].calls == 1  # 與 baseline 相同


@pytest.mark.asyncio
async def test_notes_written_and_read_back(tmp_path, monkeypatch):
    """NOTES.md：第一任務結束摘要寫入 → 第二任務實作 prompt 能讀回；結束後檔案存在。"""
    from studio import workspace

    monkeypatch.setattr(config, "NOTES_ENABLED", True)
    # 本測試驗證循序模式下「同需求內後續任務讀回前一任務寫入的 NOTES」；並行模式同波獨立任務
    # 並發、NOTES 於波末才序列化 flush（並行 NOTES 行為另由 test_parallel_waves 覆蓋），故釘循序。
    monkeypatch.setattr(config, "PARALLEL_TASKS_ENABLED", False)
    monkeypatch.setattr(config, "ENABLE_GIT", False)  # 避免依賴 git
    monkeypatch.setattr(config, "WORKSPACE_ROOT", tmp_path)
    sid = "notesflow"
    workspace.create_workspace(sid)

    bucket, broadcast = collect()
    experts = _experts(
        pm=["任務: 建立 A\n任務: 建立 B", "決議: 完成", "檢討"],
        eng=["做好了"],
        qa=["驗證: PASS"],
        senior=["決議: 核可"],
    )
    session = StudioSession(sid, broadcast, experts=experts, cwd=workspace.workspace_path(sid))
    await session.run("需求")

    # 第二任務的實作 prompt 應讀回第一任務寫入的知識庫內容
    second_prompt = experts["engineer"].prompts[1]
    assert "團隊共用知識庫" in second_prompt
    assert "建立 A" in second_prompt
    # session 結束後 NOTES.md 實際存在且含兩任務摘要，但不進交付清單
    notes = workspace.read_notes(sid)
    assert "任務 #1 完成" in notes and "任務 #2 完成" in notes
    assert "NOTES.md" not in workspace.list_files(sid)


@pytest.mark.asyncio
async def test_notes_disabled_by_default(tmp_path, monkeypatch):
    """預設不啟用：不寫 NOTES.md、實作 prompt 不含知識庫前綴。"""
    from studio import workspace

    monkeypatch.setattr(config, "ENABLE_GIT", False)
    monkeypatch.setattr(config, "WORKSPACE_ROOT", tmp_path)
    sid = "nonotes"
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
    assert "團隊共用知識庫" not in experts["engineer"].prompts[0]


@pytest.mark.asyncio
async def test_human_intervention(monkeypatch):
    """插話餵給專家；回顯（HUMAN_MESSAGE）自 #83 起改由 ws._pump_interventions 於收到時
    即時 broadcast，orchestrator 不再重複廣播。"""
    # 本測試驗證拆解階段的插話前綴：pin 掉立項澄清，避免 PM 第一句被立項消費。
    monkeypatch.setattr(config, "CLARIFY_ENABLED", False)
    bucket, broadcast = collect()
    queue: asyncio.Queue[str] = asyncio.Queue()
    queue.put_nowait("請改用公制單位")
    experts = _experts(
        pm=["任務: 實作", "決議: 完成", "檢討"],
        eng=["做好了"],
        qa=["驗證: PASS"],
        senior=["決議: 核可"],
    )
    session = StudioSession("t", broadcast, experts=experts, cwd=None, intervention_queue=queue)
    await session.run("需求")

    # 插話應前綴進 PM 的拆解 prompt（含原文）
    assert "使用者插話" in experts["pm"].prompts[0]
    assert "公制" in experts["pm"].prompts[0]
    # 回顯責任在 ws 層（收到即廣播），orchestrator 不得重複發 HUMAN_MESSAGE
    humans = [e for e in bucket if e.type == events.EventType.HUMAN_MESSAGE]
    assert humans == []


@pytest.mark.asyncio
async def test_stop_marks_stopped():
    bucket, broadcast = collect()
    experts = _experts(
        pm=["任務: 實作", "決議: 完成", "檢討"],
        eng=["做好了"],
        qa=["驗證: PASS"],
        senior=["決議: 核可"],
    )
    session = StudioSession("t", broadcast, experts=experts, cwd=None)
    session.request_stop()
    await session.run("需求")

    done = [e for e in bucket if e.type == events.EventType.DONE][0]
    assert done.payload["stopped"] is True
    assert done.payload["completed"] is False
    # 任務迴圈被跳過，工程師不應被呼叫
    assert experts["engineer"].calls == 0


@pytest.mark.asyncio
async def test_error_is_reported_not_raised():
    bucket, broadcast = collect()

    class Boom(StubExpert):
        async def speak(self, prompt, broadcast):
            raise RuntimeError("壞掉了")

    experts = _experts(pm=["x"], eng=["x"], qa=["x"], senior=["x"])
    experts["pm"] = Boom(BY_KEY["pm"], ["x"])
    session = StudioSession("t", broadcast, experts=experts, cwd=None)
    await session.run("需求")  # 不應拋出
    assert events.EventType.ERROR in types(bucket)
