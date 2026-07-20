"""PM workflow 分診（_select_workflow）：flag 護欄、marker 解析、白名單、失敗收斂、接線。

背景：workflow 原本由使用者 UI / TI_DEFAULT_WORKFLOW 開場定死，autopilot 一律走
default_workflow 安全骨架。TI_AUTOPILOT_WORKFLOW_TRIAGE 開啟後，任務開場前由 PM
（providers.complete_once 的 MODEL_FAST 一次性呼叫）依任務性質選內建流程——
小任務走「快速模式」省三審、高風險走「預設流程」。護欄不變式：任何失敗（flag 關/
LLM 錯誤/逾時/非法名稱）都回 (None, ...)，StudioSession(workflow=None) 與現行為等價。
"""

from __future__ import annotations

import pytest

from studio import autopilot, config, flow, workflow

_TASK = {"id": 7, "title": "修 README 錯字", "detail": "docs 小修"}


# --- marker 解析（flow 層） ------------------------------------------------


def test_parse_workflow_choice_half_and_full_colon():
    assert flow.parse_workflow_choice("流程: 快速模式") == "快速模式"
    assert flow.parse_workflow_choice("流程：動態優先") == "動態優先"


def test_parse_workflow_choice_takes_last_match_and_empty():
    text = "流程: 動態優先\n重新考慮後\n流程: 快速模式"
    assert flow.parse_workflow_choice(text) == "快速模式"
    assert flow.parse_workflow_choice("沒有標記的輸出") == ""
    assert flow.parse_triage_reason("理由: 單檔小修\n流程: 快速模式") == "單檔小修"


def test_parse_workflow_choice_ignores_inline_marker_text():
    text = "請不要在說明句中寫 流程: 快速模式\n理由也不要用句中 理由: 單檔小修"
    assert flow.parse_workflow_choice(text) == ""
    assert flow.parse_triage_reason(text) == ""


# --- _select_workflow 護欄 --------------------------------------------------


def _stub_once(monkeypatch, reply: str, calls: list | None = None):
    async def fake_once(system, user, *, session_id, cwd, timeout):
        if calls is not None:
            calls.append({"user": user, "session_id": session_id, "timeout": timeout})
        return reply

    from studio import providers

    monkeypatch.setattr(providers, "complete_once", fake_once)


@pytest.mark.asyncio
async def test_flag_off_makes_no_call(monkeypatch):
    """預設關閉：不發 LLM 呼叫（零成本），回 (None, "")＝現行為。"""
    monkeypatch.setattr(config, "AUTOPILOT_WORKFLOW_TRIAGE", False)
    calls: list = []
    _stub_once(monkeypatch, "流程: 快速模式", calls)
    wf, reason = await autopilot._select_workflow(_TASK, "/tmp/clone", "s1")
    assert wf is None and reason == ""
    assert calls == []


@pytest.mark.asyncio
async def test_fast_track_selected(monkeypatch):
    """命中「快速模式」→ 回 fast_track 工廠 dict＋理由；輸入含 title+detail、timeout 走設定。"""
    monkeypatch.setattr(config, "AUTOPILOT_WORKFLOW_TRIAGE", True)
    monkeypatch.setattr(config, "AUTOPILOT_TRIAGE_TIMEOUT", 45)
    calls: list = []
    _stub_once(monkeypatch, "理由: 單檔文案小修\n流程: 快速模式", calls)
    wf, reason = await autopilot._select_workflow(_TASK, "/tmp/clone", "s1")
    assert wf is not None and wf["name"] == workflow.FAST_TRACK_NAME
    assert reason == "單檔文案小修"
    assert calls[0]["timeout"] == 45.0
    assert "修 README 錯字" in calls[0]["user"] and "docs 小修" in calls[0]["user"]
    assert calls[0]["session_id"] == "s1:triage"


@pytest.mark.asyncio
async def test_whitelist_rejects_unknown_and_default(monkeypatch):
    """自創名稱不入白名單 → None；「預設流程」也回 None（等價路徑，不另建 dict）。"""
    monkeypatch.setattr(config, "AUTOPILOT_WORKFLOW_TRIAGE", True)
    _stub_once(monkeypatch, "理由: x\n流程: 我自創的流程")
    wf, _ = await autopilot._select_workflow(_TASK, "/tmp/clone", "s1")
    assert wf is None
    _stub_once(monkeypatch, "理由: 高風險\n流程: 預設流程")
    wf, reason = await autopilot._select_workflow(_TASK, "/tmp/clone", "s1")
    assert wf is None and reason == "高風險"


@pytest.mark.asyncio
async def test_empty_reply_falls_back(monkeypatch):
    """complete_once 回空字串（逾時/離線/LLM 錯誤的統一表徵）→ (None, "")。"""
    monkeypatch.setattr(config, "AUTOPILOT_WORKFLOW_TRIAGE", True)
    _stub_once(monkeypatch, "")
    wf, reason = await autopilot._select_workflow(_TASK, "/tmp/clone", "s1")
    assert wf is None and reason == ""


@pytest.mark.asyncio
async def test_file_override_cannot_hijack(monkeypatch):
    """get_workflow 被檔案定義蓋掉也不影響分診——白名單直取內建工廠。"""
    monkeypatch.setattr(config, "AUTOPILOT_WORKFLOW_TRIAGE", True)
    monkeypatch.setattr(
        workflow, "get_workflow", lambda *_a, **_k: {"name": "被劫持", "stages": []}
    )
    _stub_once(monkeypatch, "理由: x\n流程: 快速模式")
    wf, _ = await autopilot._select_workflow(_TASK, "/tmp/clone", "s1")
    assert wf is not None and wf["name"] == workflow.FAST_TRACK_NAME
    assert wf["stages"]  # 真工廠產物，不是被劫持的空殼


@pytest.mark.asyncio
async def test_exception_never_propagates(monkeypatch):
    """complete_once 意外 raise（防禦性情境）→ 兜底回 (None, "")，絕不外洩。"""
    monkeypatch.setattr(config, "AUTOPILOT_WORKFLOW_TRIAGE", True)

    async def boom(*_a, **_k):
        raise RuntimeError("上游炸掉")

    from studio import providers

    monkeypatch.setattr(providers, "complete_once", boom)
    wf, reason = await autopilot._select_workflow(_TASK, "/tmp/clone", "s1")
    assert wf is None and reason == ""


# --- run_one_task 接線 -------------------------------------------------------


def _wire_run_one_task(monkeypatch, tmp_path, captured: dict):
    """最小 mock 集（仿 test_autopilot_done_fields._common_mocks），捕捉 workflow kwarg。"""
    clone = tmp_path / "clone"
    clone.mkdir(exist_ok=True)

    async def fake_prepare_clone():
        return clone

    class FakeSession:
        def __init__(self, *_args, **kwargs):
            captured["workflow"] = kwargs.get("workflow", "MISSING")

        async def run(self, _requirement):
            return {"completed": True, "followups": [], "followup_items": [], "core_changes": []}

    async def fake_gate(*_a, **_k):
        return (True, "")

    async def fake_merge(*_a, **_k):
        return autopilot.MergeResult(True, "已合併", pr_number=1, branch="b")

    async def fake_idle():
        return True

    async def fake_redeploy():
        return (True, "ok")

    monkeypatch.setattr(autopilot, "_prepare_clone", fake_prepare_clone)
    monkeypatch.setattr(autopilot, "StudioSession", FakeSession)
    monkeypatch.setattr(autopilot.history, "start_session", lambda *a, **k: None)
    monkeypatch.setattr(autopilot.history, "record_event", lambda *a, **k: None)
    monkeypatch.setattr(autopilot.history, "finish_session", lambda *a, **k: None)
    monkeypatch.setattr(autopilot.backlog, "set_status", lambda *a, **k: None)
    monkeypatch.setattr(autopilot.backlog, "annotate", lambda *a, **k: None)
    monkeypatch.setattr(autopilot.backlog, "add_items", lambda *a, **k: 0)
    monkeypatch.setattr(autopilot.backlog, "add_many", lambda *a, **k: 0)
    monkeypatch.setattr(autopilot.backlog, "add", lambda *a, **k: None)
    monkeypatch.setattr(autopilot.backlog, "route_core_changes", lambda *a, **k: 0)
    monkeypatch.setattr(autopilot, "_gate_lint", fake_gate)
    monkeypatch.setattr(autopilot, "_gate_collect_without_sdk", fake_gate)
    monkeypatch.setattr(autopilot, "_gate_tests", fake_gate)
    monkeypatch.setattr(autopilot, "_commit_push_merge", fake_merge)
    monkeypatch.setattr(autopilot, "_wait_until_idle", fake_idle)
    monkeypatch.setattr(autopilot.deploy, "redeploy", fake_redeploy)


@pytest.mark.asyncio
async def test_run_one_task_passes_selected_workflow(monkeypatch, tmp_path):
    """flag on＋分診命中 → StudioSession 收到 fast_track dict。"""
    captured: dict = {}
    _wire_run_one_task(monkeypatch, tmp_path, captured)
    monkeypatch.setattr(config, "AUTOPILOT_WORKFLOW_TRIAGE", True)
    _stub_once(monkeypatch, "理由: 小修\n流程: 快速模式")
    await autopilot.run_one_task(dict(_TASK))
    assert captured["workflow"]["name"] == workflow.FAST_TRACK_NAME


@pytest.mark.asyncio
async def test_run_one_task_default_when_flag_off(monkeypatch, tmp_path):
    """flag off → StudioSession 收到 workflow=None（現行為）。"""
    captured: dict = {}
    _wire_run_one_task(monkeypatch, tmp_path, captured)
    monkeypatch.setattr(config, "AUTOPILOT_WORKFLOW_TRIAGE", False)
    await autopilot.run_one_task(dict(_TASK))
    assert captured["workflow"] is None


@pytest.mark.asyncio
async def test_run_one_task_survives_triage_crash(monkeypatch, tmp_path):
    """_select_workflow 整個炸掉（monkeypatch 成必炸版本）→ 任務照常跑完、workflow=None。

    實作內已有 try/except 兜底；此測試把函式本體換成必炸版本，守護「接線端不得假設
    分診永不失敗」的防禦深度——分診是加值不是依賴，失敗絕不可擋任務執行。
    """
    captured: dict = {}
    _wire_run_one_task(monkeypatch, tmp_path, captured)

    async def boom(*_a, **_k):
        raise RuntimeError("分診炸掉")

    monkeypatch.setattr(autopilot, "_select_workflow", boom)
    await autopilot.run_one_task(dict(_TASK))  # 不得 raise
    assert captured["workflow"] is None
