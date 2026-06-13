"""任務 #5 冒煙：fake experts 走「需求→議程拆解→分派→逐子題討論→彙整」全流程（離線）。

用假專家驅動真實 server→ws→orchestrator 管線，引擎模式（TI_DISCUSS_MODE=round_robin）下驗證：
- agenda_plan 事件回指本場 fake PM 腳本的子題（自證對應，排除假綠）；
- assignee 硬驗證在真實管線生效：`負責: architect`（本場缺席）fallback engineer 且修正入事件；
- 逐子題討論真的發生（phase 事件＋討論期間 engineer/senior 有發言）；
- 彙整與既有流程零回歸（任務全完成、檔案落地、Demo 通過、history 可重看 agenda_plan）。
另附 legacy 反向對照：同腳本不開引擎模式，絕不出現逐子題討論 phase（證明非假綠）。
不需 API 金鑰。
"""

from __future__ import annotations

import json
import re

import pytest
from fastapi.testclient import TestClient

from studio import config, workspace

REQUIREMENT = "做一個四則運算 CLI"
# fake PM 腳本宣告的議程（fake_experts._pm_decompose_script 循序分支）——驗證輸出須回指這份輸入。
EXPECTED_TITLES = ["核心運算模組", "介面與說明"]


def _make_client(tmp_path, monkeypatch, discuss_mode: str) -> TestClient:
    monkeypatch.setattr(config, "ACCESS_PASSWORD", "")
    monkeypatch.setattr(config, "OFFLINE_MODE", True)
    monkeypatch.setattr(config, "PARALLEL_TASKS_ENABLED", False)
    monkeypatch.setattr(config, "OFFLINE_DELAY", 0.0)
    monkeypatch.setattr(config, "DEBATE_ROUNDS", 1)
    monkeypatch.setattr(config, "DISCUSS_MODE", discuss_mode)
    monkeypatch.setattr(config, "AGENDA_ROUNDS", 1)
    # 與 test_offline_e2e 同款：學習機制 pin 關，驗證確定性產出。
    monkeypatch.setattr(config, "HUDDLE_ENABLED", False)
    monkeypatch.setattr(config, "REFLEXION_ENABLED", False)
    monkeypatch.setattr(config, "SELF_REFINE_ITERS", 0)
    monkeypatch.setattr(config, "OBJECTIVE_GATE", "0")
    monkeypatch.setattr(config, "WORKSPACE_ROOT", tmp_path / "ws")
    monkeypatch.setattr(config, "HISTORY_ROOT", tmp_path / "hist")
    from studio.server import app

    return TestClient(app, client=("127.0.0.1", 12345))


@pytest.fixture
def client(tmp_path, monkeypatch):
    return _make_client(tmp_path, monkeypatch, "round_robin")


@pytest.fixture
def legacy_client(tmp_path, monkeypatch):
    return _make_client(tmp_path, monkeypatch, "legacy")


def _run_session(client: TestClient, requirement: str) -> list[dict]:
    evs: list[dict] = []
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"requirement": requirement})
        for _ in range(800):  # 上限保護
            ev = ws.receive_json()
            evs.append(ev)
            if ev["type"] in ("done", "error"):
                break
    return evs


def _by_type(evs: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for e in evs:
        out.setdefault(e["type"], []).append(e)
    return out


def test_agenda_full_flow_engine_mode(client):
    evs = _run_session(client, REQUIREMENT)
    by_type = _by_type(evs)

    # 0) 全流程跑完且驗收通過（無未捕捉例外 → 不會收到 error）
    assert "error" not in by_type
    done = by_type["done"][-1]
    assert done["payload"]["completed"] is True
    sid = done["session_id"]

    # 1) 議程拆解＋分派：agenda_plan 事件回指 fake PM 腳本（自證對應）
    plans = by_type.get("agenda_plan", [])
    assert len(plans) == 1, "拆解後應 broadcast 恰一筆 agenda_plan"
    plan = plans[0]["payload"]
    assert [a["title"] for a in plan["agenda"]] == EXPECTED_TITLES
    # 子題標題確實出自本場 PM 的發言（排除假綠：不是 parser 憑空生出）
    pm_texts = [
        e["payload"]["text"] for e in by_type["expert_message"] if e["payload"]["speaker"] == "pm"
    ]
    assert all(any(t in txt for txt in pm_texts) for t in EXPECTED_TITLES)
    # 硬驗證：engineer 合法照分派；architect 本場缺席 → fallback engineer ＋修正紀錄
    assert [a["assignee"] for a in plan["assignments"]] == ["engineer", "engineer"]
    assert plan["corrections"] == [{"index": 1, "given": "architect", "assigned": "engineer"}]
    assert len(plan["tasks"]) == 3  # 任務清單同快照（沿用既有 parse_tasks）

    # 2) 逐子題討論真的發生：phase 事件＋兩個子題期間 engineer/senior 都有發言
    phases = [(e["payload"]["phase"], e["payload"]["detail"]) for e in by_type["phase_change"]]
    assert ("架構討論", "逐子題多角色討論（round_robin，2 個子題）") in phases
    idx = next(
        i
        for i, e in enumerate(evs)
        if e["type"] == "phase_change" and e["payload"]["phase"] == "架構討論"
    )
    nxt = next(i for i, e in enumerate(evs) if i > idx and e["type"] == "phase_change")
    speakers = [e["payload"]["speaker"] for e in evs[idx:nxt] if e["type"] == "expert_message"]
    # 2 子題 × 1 輪 × (主責 engineer ＋ senior) = engineer/senior 各 2 次發言
    assert speakers.count("engineer") == 2 and speakers.count("senior") == 2

    # 3) 彙整與既有流程零回歸：任務全完成、檔案落地、Demo 真的算出 7.0
    done_tasks = [e for e in by_type.get("task_status", []) if e["payload"]["status"] == "done"]
    assert len({e["payload"]["id"] for e in done_tasks}) == 3
    files = workspace.list_files(sid)
    assert {"calculator.py", "main.py", "README.md", "test_calculator.py"} <= set(files)
    demo = by_type["demo_result"][-1]
    assert demo["payload"]["passed"] is True and "7.0" in demo["payload"]["output"]

    # 4) 可重看：history 重播含同一筆 agenda_plan（議程/分派/修正俱在）
    replay = client.get(f"/api/history/{sid}/events").json()["events"]
    assert len(replay) == len(evs)
    saved = [e for e in replay if e["type"] == "agenda_plan"]
    assert len(saved) == 1 and saved[0]["payload"] == plan

    # 5) 結論彙整落盤（任務 #4 驗收 #4/#5/#7）：CONCLUSION.md 產出、四段齊全、進 git、
    #    至少一條結論帶 (round, speaker) 錨點且回指 transcript 實際發言（自證對應、排除假綠）。
    md = (config.WORKSPACE_ROOT / sid / "CONCLUSION.md").read_text(encoding="utf-8")
    for header in ("## 共識", "## 分歧", "## 未決事項", "## 後續行動"):
        assert header in md, f"CONCLUSION.md 缺少 {header} 段"

    # 5a) 已被 git commit：有「結論彙整」commit 事件，且 broadcast 一筆 conclusion 事件。
    commit_msgs = [e["payload"]["message"] for e in by_type.get("git_commit", [])]
    assert any("結論彙整" in m for m in commit_msgs), "CONCLUSION.md 應有對應 git commit"
    conc_evs = by_type.get("conclusion", [])
    assert len(conc_evs) == 1, "應 broadcast 恰一筆 conclusion 事件"
    assert conc_evs[0]["payload"]["path"].endswith("CONCLUSION.md")
    assert set(conc_evs[0]["payload"]["summary"]) == {
        "consensus",
        "disagreements",
        "open_questions",
        "actions",
    }

    # 5b) 自證對應：抽一條帶 (R<round> <speaker>) 錨點的結論，反查該輪該角色確有此 mention。
    anchors = re.findall(r"\(R(\d+)\s+([^\s)]+)\)", md)
    assert anchors, "CONCLUSION.md 至少一條結論須帶 (round, speaker) 錨點"
    round_no, speaker = anchors[0]
    # 錨點 speaker 必為本場討論真實參與者，且其發言確含結構化引用（mention）。
    spoken = [
        e["payload"]["text"] for e in by_type["expert_message"] if e["payload"]["name"] == speaker
    ]
    assert spoken, f"錨點 speaker={speaker} 並非本場發言者（假綠）"
    assert any("回應 @" in t for t in spoken), (
        "錨點所指角色的發言應含結構化引用，方能回指 transcript"
    )

    # 5c) 反向防幻覺：共識段每條結論都能回指規則層（帶錨點或標「（無）」），
    #     不得出現 transcript 未產生的憑空 mention。
    consensus_block = md.split("## 共識", 1)[1].split("##", 1)[0]
    for line in consensus_block.splitlines():
        line = line.strip()
        if not line.startswith("- ") or line == "- （無）":
            continue
        assert "(R" in line, f"共識結論缺錨點、疑似幻覺：{line}"

    # 5d) 機讀 sidecar（任務 #3＋#4 接線）：conclusion.json 與 md 同場落盤、schema 完整，
    #     且 rounds 為真實輪數——攔截「orchestrator 漏傳 rounds → 恆為 0」的接線缺口。
    sidecar = config.WORKSPACE_ROOT / sid / "conclusion.json"
    assert sidecar.is_file(), "conclusion.json sidecar 應與 CONCLUSION.md 同場落盤"
    data = json.loads(sidecar.read_text(encoding="utf-8"))  # 合法 JSON
    assert data["version"] == 1
    assert data["session_id"] == sid
    assert set(data) == {
        "version",
        "session_id",
        "rounds",
        "consensus",
        "disagreements",
        "open_questions",
        "actions",
    }
    # 真實輪數＝md 錨點觀察到的最大 round（來自 transcript Utterance.round）；rounds 必 ≥1
    # 且等於該值——若 orchestrator 漏傳 rounds，這裡會是 0、斷言失敗（自證對應、排除假綠）。
    max_anchor_round = max(int(r) for r, _ in anchors)
    assert data["rounds"] == max_anchor_round, (
        f"sidecar rounds={data['rounds']} 應等於真實輪數 {max_anchor_round}（非寫死 0）"
    )
    assert data["rounds"] >= 1


def test_agenda_legacy_negative_control(legacy_client):
    """反向對照：同一份 fake PM 腳本、不開引擎模式——agenda_plan 照樣持久化（任務 #4），
    但絕不出現逐子題討論 phase（證明引擎 phase 事件非假綠）、流程零回歸跑完。"""
    evs = _run_session(legacy_client, REQUIREMENT)
    by_type = _by_type(evs)
    assert by_type["done"][-1]["payload"]["completed"] is True
    assert len(by_type.get("agenda_plan", [])) == 1
    details = [e["payload"]["detail"] for e in by_type["phase_change"]]
    assert not any("逐子題" in d for d in details)
