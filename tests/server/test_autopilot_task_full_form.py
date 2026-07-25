"""完整下任務表單(功能強化 C2):POST /api/autopilot/task 擴 detail/priority/type。

守護不變量:舊 client 只送 title 行為不變(Pydantic 預設值向後相容);priority 超界由
backlog._clamp_priority 夾 0-2;type 亂值由 _norm_type 正規化 improvement;重複標題 400;
detail 夾 4000 字防灌爆單一 JSON 檔。
"""

from __future__ import annotations

import pytest

from studio import admission_mode, backlog, config, routes


@pytest.fixture(autouse=True)
def _state(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_STATE_DIR", tmp_path / "ap")
    monkeypatch.setattr(backlog, "_read_cache", {}, raising=False)
    admission_mode.bootstrap_at_task_boundary(
        "shadow",
        initial_effective="shadow",
        release_holds=lambda _mode: 0,
    )
    return tmp_path


@pytest.mark.asyncio
async def test_title_only_backward_compatible():
    resp = await routes.autopilot_add_task(routes.TaskBody(title="只有標題"))
    assert resp.status_code == 200
    t = backlog.list_tasks("pending")[0]
    assert t["title"] == "只有標題"
    assert t["priority"] == 1 and t["type"] == "improvement", "預設值與舊行為一致"


@pytest.mark.asyncio
async def test_full_form_fields_land():
    resp = await routes.autopilot_add_task(
        routes.TaskBody(title="完整任務", detail="驗收:X", priority=0, type="bug")
    )
    assert resp.status_code == 200
    t = backlog.list_tasks("pending")[0]
    assert t["detail"] == "驗收:X" and t["priority"] == 0 and t["type"] == "bug"


@pytest.mark.asyncio
async def test_versioned_contract_is_evaluated_and_returned():
    body = routes.TaskBody(
        title="完整契約任務",
        risk="low",
        contract={
            "version": 1,
            "outcome": "backlog 可安全排序",
            "kind": "implementation",
            "targets": ["studio/backlog.py"],
            "acceptance": ["pytest tests/core/test_backlog.py -q", "reviewable diff"],
        },
    )

    response = await routes.autopilot_add_task(body)
    task = backlog.list_tasks("pending")[0]

    assert response.status_code == 200
    assert task["contract"]["version"] == 1
    assert task["admission"]["outcome"] == "ready"
    assert task["admission"]["phase"] == "enqueue"


@pytest.mark.asyncio
async def test_priority_clamped_and_type_normalized():
    await routes.autopilot_add_task(routes.TaskBody(title="怪值任務", priority=5, type="junk"))
    t = backlog.list_tasks("pending")[0]
    assert t["priority"] == 2, "超界 priority 夾到 2"
    assert t["type"] == "improvement", "亂 type 正規化"


@pytest.mark.asyncio
async def test_duplicate_title_400_and_detail_truncated():
    await routes.autopilot_add_task(routes.TaskBody(title="同名"))
    dup = await routes.autopilot_add_task(routes.TaskBody(title="同名"))
    assert dup.status_code == 400

    await routes.autopilot_add_task(routes.TaskBody(title="長細節", detail="x" * 5000))
    t = next(t for t in backlog.list_tasks("pending") if t["title"] == "長細節")
    assert len(t["detail"]) == 4000, "detail 夾 4000 防灌爆 backlog.json"


@pytest.mark.asyncio
async def test_corrupt_admission_mode_state_rejects_manual_intake():
    (config.AUTOPILOT_STATE_DIR / "admission_mode.json").write_text(
        "{broken",
        encoding="utf-8",
    )

    response = await routes.autopilot_add_task(routes.TaskBody(title="不可入列"))

    assert response.status_code == 503
    assert b'"detail"' in response.body
    assert backlog.list_tasks() == []


@pytest.mark.asyncio
async def test_missing_admission_mode_state_rejects_manual_intake():
    (config.AUTOPILOT_STATE_DIR / "admission_mode.json").unlink()

    response = await routes.autopilot_add_task(routes.TaskBody(title="不可偷渡初始化"))

    assert response.status_code == 503
    assert b'"detail"' in response.body
    assert backlog.list_tasks() == []
