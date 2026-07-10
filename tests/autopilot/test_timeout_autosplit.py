"""硬牆逾時任務自動拆分（完成率第二輪修法 ⑤）。

背景（完成率診斷）：AUTOPILOT_TASK_TIMEOUT 硬牆逾時的任務（多半範圍太大跑不完）舊行為＝直接
parked「需拆分或縮小範圍」等人工，長期躺成死水（live 現有 6 筆）。本修法讓逾時任務自動交資深專家
拆成更小、可獨立出貨的子任務再排回 backlog、原任務歸檔 parked，並以 split_depth 逐代封頂避免無限拆分。

純檔案 IO + monkeypatch（mock Expert.speak / _prepare_clone），不打 LLM/網路。
"""

from __future__ import annotations

import pytest

from studio import autopilot, backlog, config


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_STATE_DIR", tmp_path / "ap")
    monkeypatch.setattr(config, "AUTOPILOT_DRYRUN", False)
    monkeypatch.setattr(config, "AUTOPILOT_TIMEOUT_AUTOSPLIT", True)
    monkeypatch.setattr(config, "AUTOPILOT_SPLIT_MAX_DEPTH", 2)
    monkeypatch.setattr(config, "AUTOPILOT_SPLIT_MAX_SUBTASKS", 4)
    monkeypatch.setattr(config, "AUTOPILOT_TASK_TIMEOUT", 7200)
    return tmp_path


def _patch_expert(monkeypatch, reply: str):
    import studio.experts as experts_mod

    class _FakeExpert:
        def __init__(self, *a, **k):
            pass

        async def speak(self, prompt, on_event):
            type(self).last_prompt = prompt
            return reply

        async def stop(self):
            return None

    monkeypatch.setattr(experts_mod, "Expert", _FakeExpert)
    return _FakeExpert


async def _fake_clone(*_a, **_k):
    return "/tmp/does-not-matter"


# --- _build_split_prompt ---------------------------------------------------


def test_build_split_prompt_includes_task_and_format():
    p = autopilot._build_split_prompt({"title": "重構 orchestrator 派工", "detail": "細節 X"})
    assert "重構 orchestrator 派工" in p and "細節 X" in p
    assert "任務: " in p, "須指示 parse_tasks 可讀的『任務: 』格式"
    assert "更小" in p


# --- _autosplit_task -------------------------------------------------------


@pytest.mark.asyncio
async def test_autosplit_parses_and_caps(state, monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_SPLIT_MAX_SUBTASKS", 2)
    _patch_expert(
        monkeypatch,
        "任務: 實作 A 子模組並補單測\n任務: 修復 B 的競態並加守門測試\n任務: 重構 C 介面",
    )
    out = await autopilot._autosplit_task("/clone", {"title": "大任務", "detail": ""})
    assert out == ["實作 A 子模組並補單測", "修復 B 的競態並加守門測試"], (
        "解析 + 截斷到 MAX_SUBTASKS"
    )


@pytest.mark.asyncio
async def test_autosplit_keeps_same_subsystem_children(state, monkeypatch):
    """拆分刻意產出多個同子系統的更小項——不得被子系統覆蓋上限誤殺（不走 _screen_followups 全套）。"""
    monkeypatch.setattr(config, "AUTOPILOT_SUBSYSTEM_MAX_PENDING", 2)  # 若誤用會把第 3 筆起擋掉
    # 先塞既有 backlog 排隊項，模擬同子系統已有存量
    for title in ("重構 backlog 載入", "優化 backlog 查詢"):
        t = backlog.add(title)
        backlog.set_status(t["id"], "pending")
    _patch_expert(
        monkeypatch,
        "任務: 實作 backlog 分頁載入並補測\n"
        "任務: 修復 backlog 併發寫入競態並補測\n"
        "任務: 重構 backlog 索引結構並補測",
    )
    out = await autopilot._autosplit_task("/clone", {"title": "重寫整個 backlog 子系統"})
    assert len(out) == 3, "三個同子系統子任務全數保留（子系統上限不該作用於拆分）"


@pytest.mark.asyncio
async def test_autosplit_drops_fallback_and_original_and_busywork(state, monkeypatch):
    """空回應 fallback『實作需求』、與原任務同名、以及無價值元任務都不該成為子任務。"""
    _patch_expert(
        monkeypatch,
        (
            "任務: 實作需求\n"  # parse_tasks 空 fallback 的雜訊
            "任務: 大任務\n"  # 原任務標題原封回填
            "任務: 收尾驗收單一 QA pass 並落檔 sha256\n"  # 價值閘 busywork
            "任務: 實作 D 快取層並補單測\n"  # 唯一合法子任務
        ),
    )
    out = await autopilot._autosplit_task("/clone", {"title": "大任務", "detail": ""})
    assert out == ["實作 D 快取層並補單測"]


# --- _handle_task_timeout --------------------------------------------------


def _timed_out_task(depth: int = 0):
    t = backlog.add("範圍過大的任務")
    if depth:
        backlog.set_status(t["id"], "pending", split_depth=depth)
        t["split_depth"] = depth
    return t


@pytest.mark.asyncio
async def test_timeout_autosplit_creates_children_and_parks_original(state, monkeypatch):
    monkeypatch.setattr(autopilot, "_prepare_clone", _fake_clone)
    _patch_expert(monkeypatch, "任務: 實作 A 並補測\n任務: 修復 B 並補測")
    task = _timed_out_task()

    await autopilot._handle_task_timeout(task)

    tasks = {t["title"]: t for t in backlog.list_tasks()}
    # 原任務歸檔 parked、note 指向子任務
    orig = tasks["範圍過大的任務"]
    assert orig["status"] == "parked" and "已自動拆為" in orig["note"]
    # 兩個子任務為 pending、source=split、split_depth=1
    children = [t for t in backlog.list_tasks() if t.get("source") == "split"]
    assert {c["title"] for c in children} == {"實作 A 並補測", "修復 B 並補測"}
    assert all(c["status"] == "pending" and c.get("split_depth") == 1 for c in children)


@pytest.mark.asyncio
async def test_timeout_depth_cap_no_split(state, monkeypatch):
    """達拆分深度上限 → 不再自動拆、維持 parked（含深度上限說明）。"""
    monkeypatch.setattr(autopilot, "_prepare_clone", _fake_clone)
    fake = _patch_expert(monkeypatch, "任務: 不該被產生的子任務")
    task = _timed_out_task(depth=2)  # == MAX_DEPTH

    await autopilot._handle_task_timeout(task)

    orig = next(t for t in backlog.list_tasks() if t["title"] == "範圍過大的任務")
    assert orig["status"] == "parked" and "深度上限" in orig["note"]
    assert not [t for t in backlog.list_tasks() if t.get("source") == "split"], "達上限不得再拆"
    assert not hasattr(fake, "last_prompt"), "達上限根本不該叫專家"


@pytest.mark.asyncio
async def test_timeout_autosplit_disabled_keeps_old_parked(state, monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_TIMEOUT_AUTOSPLIT", False)
    monkeypatch.setattr(autopilot, "_prepare_clone", _fake_clone)
    _patch_expert(monkeypatch, "任務: 子任務")
    task = _timed_out_task()

    await autopilot._handle_task_timeout(task)

    orig = next(t for t in backlog.list_tasks() if t["title"] == "範圍過大的任務")
    assert orig["status"] == "parked" and "需拆分或縮小範圍" in orig["note"]
    assert not [t for t in backlog.list_tasks() if t.get("source") == "split"]


@pytest.mark.asyncio
async def test_timeout_empty_split_falls_back_to_parked(state, monkeypatch):
    """專家拆不出有效子任務（全是雜訊/busywork）→ 退回舊 parked 行為，不留半吊子。"""
    monkeypatch.setattr(autopilot, "_prepare_clone", _fake_clone)
    _patch_expert(monkeypatch, "任務: 實作需求\n任務: 收尾驗收 QA pass 落檔 sha256")
    task = _timed_out_task()

    await autopilot._handle_task_timeout(task)

    orig = next(t for t in backlog.list_tasks() if t["title"] == "範圍過大的任務")
    assert orig["status"] == "parked" and "需拆分或縮小範圍" in orig["note"]
    assert not [t for t in backlog.list_tasks() if t.get("source") == "split"]


@pytest.mark.asyncio
async def test_timeout_split_exception_falls_back_to_parked(state, monkeypatch):
    """拆分過程拋例外也不得中斷主迴圈：吞掉、退回 parked。"""

    async def _boom(*_a, **_k):
        raise RuntimeError("clone 掛了")

    monkeypatch.setattr(autopilot, "_prepare_clone", _boom)
    task = _timed_out_task()

    await autopilot._handle_task_timeout(task)  # 不得拋出

    orig = next(t for t in backlog.list_tasks() if t["title"] == "範圍過大的任務")
    assert orig["status"] == "parked" and "需拆分或縮小範圍" in orig["note"]
