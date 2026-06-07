"""QA 驗收：任務 #4「停滯守門」驗收標準專測。

驗收標準：
1. 模擬連續兩輪無實質進展 → 迴圈提早結束（不跑滿全部輪數）。
2. 發出可觀察事件（phase_change「停滯收斂」）。
3. 有測試覆蓋。
補充：有實質進展不誤觸、commit 變動視為有進展、cwd=None/關 git 不偵測、停滯寫入 NOTES。
"""

from __future__ import annotations

import pytest

from studio import config, events, runner, workspace
from studio.orchestrator import StudioSession, is_stalled, text_similarity
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


def _git_off(monkeypatch):
    """讓 git 變成 no-op（commit 不產生 hash → 視為無檔案變動）。"""

    async def _noop_init(cwd):
        return True

    async def _noop_commit(cwd, message):
        return None

    monkeypatch.setattr(runner, "git_init", _noop_init)
    monkeypatch.setattr(runner, "git_commit", _noop_commit)
    monkeypatch.setattr(config, "ENABLE_GIT", True)


# === 純函式：相似度與停滯判定 =========================================


def test_text_similarity_bounds():
    assert text_similarity("一樣", "一樣") == 1.0
    assert text_similarity("", "") == 1.0
    assert text_similarity("完全不同 ABCDEF", "毫不相干 XYZUVW") < 0.5


def test_is_stalled_basic():
    # 連續兩輪幾乎相同 → 停滯
    assert is_stalled(["改 X", "內容相同的一句話", "內容相同的一句話"], rounds=2)
    # 有實質差異 → 不停滯
    assert not is_stalled(["第一版實作方案甲", "完全改寫的方案乙"], rounds=2)


def test_is_stalled_guards():
    # rounds<=1 不判定
    assert not is_stalled(["a", "a"], rounds=1)
    # 歷史不足 rounds 筆 → 不判定（避免一開始誤觸）
    assert not is_stalled(["a"], rounds=2)
    assert not is_stalled([], rounds=2)


def test_is_stalled_threshold_three_rounds():
    same = "同一段沒有任何進展的發言內容"
    assert is_stalled([same, same, same], rounds=3)
    # 三輪中有一輪不同 → 不算停滯
    assert not is_stalled([same, same, "這輪做了完全不同的改寫嘗試"], rounds=3)


# === 驗收標準 1+2：提早收斂 + 可觀察事件 ==============================


@pytest.mark.asyncio
async def test_stall_breaks_before_max_rounds(tmp_path, monkeypatch):
    _git_off(monkeypatch)
    monkeypatch.setattr(config, "STALL_ROUNDS", 2)
    monkeypatch.setattr(config, "TASK_MAX_ROUNDS", 6)
    monkeypatch.setattr(config, "WORKSPACE_ROOT", tmp_path)
    sid = "stall1"
    workspace.create_workspace(sid)

    bucket, broadcast = collect()
    experts = _experts(
        pm=["任務: 實作", "決議: 未完成", "檢討"],
        eng=["我用同樣的做法，沒有任何改變"],  # 每輪重述
        qa=["驗證: FAIL"],
        senior=["決議: 退回"],
    )
    session = StudioSession(sid, broadcast, experts=experts, cwd=workspace.workspace_path(sid))
    await session.run("需求")

    # 連續 2 輪重述 → 第 2 輪即收斂，遠少於 6 輪
    assert experts["engineer"].calls == 2
    phases = [e.payload["phase"] for e in bucket if e.type == events.EventType.PHASE_CHANGE]
    assert "停滯收斂" in phases  # 可觀察事件
    # 停滯收斂任務未通過 → 維持 review，不誤標 done
    assert session._tasks[0]["status"] == "review"


# === 有實質進展不誤觸 ================================================


@pytest.mark.asyncio
async def test_no_stall_when_content_changes(tmp_path, monkeypatch):
    """每輪發言都不同（有進展）→ 不提早收斂，跑滿輪數。"""
    _git_off(monkeypatch)
    monkeypatch.setattr(config, "STALL_ROUNDS", 2)
    monkeypatch.setattr(config, "TASK_MAX_ROUNDS", 3)
    monkeypatch.setattr(config, "WORKSPACE_ROOT", tmp_path)
    sid = "nostall"
    workspace.create_workspace(sid)

    bucket, broadcast = collect()
    experts = _experts(
        pm=["任務: 實作", "決議: 未完成", "檢討"],
        eng=[
            "第一版：先用陣列實作核心邏輯與資料結構",
            "第二版：改用雜湊表大幅優化查詢效能",
            "第三版：加入快取層與並行處理進一步改善",
        ],
        qa=["驗證: FAIL"],
        senior=["決議: 退回"],
    )
    session = StudioSession(sid, broadcast, experts=experts, cwd=workspace.workspace_path(sid))
    await session.run("需求")

    assert experts["engineer"].calls == 3  # 跑滿 3 輪
    phases = [e.payload["phase"] for e in bucket if e.type == events.EventType.PHASE_CHANGE]
    assert "停滯收斂" not in phases


# === commit 有變動視為有進展 ==========================================


@pytest.mark.asyncio
async def test_commit_change_resets_stall(tmp_path, monkeypatch):
    """即使發言相同，只要每輪有實質 commit 變動就不算停滯。"""

    async def _noop_init(cwd):
        return True

    counter = {"n": 0}

    async def _changing_commit(cwd, message):
        counter["n"] += 1
        return f"hash{counter['n']}"  # 每輪不同 hash → committed_change=True

    monkeypatch.setattr(runner, "git_init", _noop_init)
    monkeypatch.setattr(runner, "git_commit", _changing_commit)
    monkeypatch.setattr(config, "ENABLE_GIT", True)
    monkeypatch.setattr(config, "STALL_ROUNDS", 2)
    monkeypatch.setattr(config, "TASK_MAX_ROUNDS", 3)
    monkeypatch.setattr(config, "WORKSPACE_ROOT", tmp_path)
    sid = "commitprogress"
    workspace.create_workspace(sid)

    bucket, broadcast = collect()
    experts = _experts(
        pm=["任務: 實作", "決議: 未完成", "檢討"],
        eng=["同樣的發言內容重複"],  # 文字相同
        qa=["驗證: FAIL"],
        senior=["決議: 退回"],
    )
    session = StudioSession(sid, broadcast, experts=experts, cwd=workspace.workspace_path(sid))
    await session.run("需求")

    # 有檔案變動 → 不收斂，跑滿 3 輪
    assert experts["engineer"].calls == 3
    phases = [e.payload["phase"] for e in bucket if e.type == events.EventType.PHASE_CHANGE]
    assert "停滯收斂" not in phases


# === 停滯收斂寫入 NOTES ===============================================


@pytest.mark.asyncio
async def test_stall_recorded_in_notes(tmp_path, monkeypatch):
    _git_off(monkeypatch)
    monkeypatch.setattr(config, "NOTES_ENABLED", True)
    monkeypatch.setattr(config, "STALL_ROUNDS", 2)
    monkeypatch.setattr(config, "TASK_MAX_ROUNDS", 5)
    monkeypatch.setattr(config, "WORKSPACE_ROOT", tmp_path)
    sid = "stallnotes"
    workspace.create_workspace(sid)

    bucket, broadcast = collect()
    experts = _experts(
        pm=["任務: 實作", "決議: 未完成", "檢討"],
        eng=["原地踏步的相同發言"],
        qa=["驗證: FAIL"],
        senior=["決議: 退回"],
    )
    session = StudioSession(sid, broadcast, experts=experts, cwd=workspace.workspace_path(sid))
    await session.run("需求")

    notes = workspace.read_notes(sid)
    assert "停滯收斂" in notes


# === 可關閉：cwd=None / 關 git 不偵測 =================================


@pytest.mark.asyncio
async def test_no_stall_when_no_cwd():
    bucket, broadcast = collect()
    experts = _experts(
        pm=["任務: 實作", "決議: 未完成", "檢討"],
        eng=["一樣的內容重複出現"],
        qa=["驗證: FAIL"],
        senior=["決議: 退回"],
    )
    session = StudioSession("t", broadcast, experts=experts, cwd=None)
    await session.run("需求")
    assert experts["engineer"].calls == config.TASK_MAX_ROUNDS  # 不提早收斂


@pytest.mark.asyncio
async def test_no_stall_when_git_disabled(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ENABLE_GIT", False)
    monkeypatch.setattr(config, "STALL_ROUNDS", 2)
    monkeypatch.setattr(config, "TASK_MAX_ROUNDS", 3)
    monkeypatch.setattr(config, "WORKSPACE_ROOT", tmp_path)
    sid = "gitoff"
    workspace.create_workspace(sid)

    bucket, broadcast = collect()
    experts = _experts(
        pm=["任務: 實作", "決議: 未完成", "檢討"],
        eng=["重複相同發言"],
        qa=["驗證: FAIL"],
        senior=["決議: 退回"],
    )
    session = StudioSession(sid, broadcast, experts=experts, cwd=workspace.workspace_path(sid))
    await session.run("需求")
    # 關 git → _stalled 一律 False → 跑滿輪數
    assert experts["engineer"].calls == 3
    phases = [e.payload["phase"] for e in bucket if e.type == events.EventType.PHASE_CHANGE]
    assert "停滯收斂" not in phases
