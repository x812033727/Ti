"""QA 驗收：任務 #5「不破壞既有功能 + 向後相容」驗收標準專測。

驗收標準：
1. pytest -q 全綠（含 test_orchestrator/test_offline_e2e/test_roles）— 由整體套件保證。
2. 事件新增向後相容：既有事件 helper 的 payload 契約不變；新事件 to_dict 結構一致。
3. 前端 handleEvent 對未知事件不崩潰 — 用 node 載入真實 app.js 實測（無 node 則 skip）。
"""

from __future__ import annotations

import shutil
import subprocess

import pytest
from _repo import REPO_ROOT

from studio import events

ROOT = REPO_ROOT


# === 事件向後相容契約 ==================================================


def test_existing_event_payload_contracts_stable():
    """既有事件 helper 的 payload 鍵維持不變（前端依賴這些鍵）。"""
    e = events.phase_change("s", "實作", "detail")
    assert e.type == events.EventType.PHASE_CHANGE
    assert set(e.payload) == {"phase", "detail"}

    e = events.run_result("s", True, "ok", log="L")
    assert set(e.payload) == {"passed", "detail", "log"}

    e = events.demo_result("s", "cmd", 0, "out")
    assert set(e.payload) == {"label", "command", "exit_code", "passed", "output"}

    e = events.board_update("s", {"todo": []})
    assert set(e.payload) == {"columns"}

    e = events.task_status("s", 1, "T", "done")
    assert set(e.payload) == {"id", "title", "status"}

    e = events.expert_message("s", "pm", "PM", "🧑", "hi")
    assert set(e.payload) == {"speaker", "name", "avatar", "text", "streaming", "final"}


def test_new_events_have_stable_payload_shape():
    """新增事件 huddle / critic_review 的 to_dict 結構正確、可序列化。"""
    h = events.huddle("s", 1, "T", ["pm", "engineer"], "結論")
    d = h.to_dict()
    assert d["type"] == "huddle"
    assert set(d) == {"type", "session_id", "ts", "payload"}
    assert set(d["payload"]) == {"task_id", "title", "participants", "conclusion", "limitation"}
    assert d["payload"]["limitation"] is False

    h2 = events.huddle("s", 2, "T2", [], "x", limitation=True)
    assert h2.to_dict()["payload"]["limitation"] is True

    c = events.critic_review("s", "pm", True, "放行")
    cd = c.to_dict()
    assert cd["type"] == "critic_review"
    assert set(cd["payload"]) == {"gate", "passed", "text"}


def test_event_types_are_unique_strings():
    """所有事件型別值唯一且為字串（避免新增造成衝突）。"""
    values = [e.value for e in events.EventType]
    assert len(values) == len(set(values))
    assert all(isinstance(v, str) for v in values)
    # 新事件確實存在
    assert events.EventType.HUDDLE.value == "huddle"
    assert events.EventType.CRITIC_REVIEW.value == "critic_review"


# === 前端 handleEvent：未知事件不崩潰（node 實測） ====================


@pytest.mark.skipif(shutil.which("node") is None, reason="未安裝 node，略過前端測試")
def test_frontend_handleEvent_handles_all_and_unknown_events():
    """用 node 載入真實 web/app.js，驗證已知+未知事件 handleEvent 皆不拋錯。"""
    script = ROOT / "tests" / "frontend_handleEvent_test.mjs"
    assert script.is_file()
    result = subprocess.run(
        ["node", str(script)],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, f"前端測試失敗：\n{result.stdout}\n{result.stderr}"
    assert "失敗 0" in result.stdout
