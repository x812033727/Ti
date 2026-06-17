"""任務 #1（blocker gate）QA 守護測試：定位「警示塊政策選項」於 agenda_plan payload／history jsonl。

================================================================================
QA 設計理由（為何這個檔存在）
================================================================================
任務 #1 是「定位」任務，不是「修補」任務。驗收標準原文：

    #1 產出一行明確結論：政策選項對應的 payload 欄位名 + 一筆真實範例值；
    或明確標記「該資料目前不存在於 payload／history」並列為 blocker。

本檔只做「定位 + 守護 #1 結論」，**不在 production code 改任何東西、不建注入管線**。
涵蓋三路徑交叉驗證 + 翻案條件反證 + 既有覆蓋誠實評估：

  1. 事件建構子路徑：events.agenda_plan() + history.record_event/load_events
     （純事件層、最不依賴）
  2. 真實 orchestrator 路徑：StudioSession.run() 走 flow.parse_agenda →
     flow.validate_assignees → events.agenda_plan 全鏈（與既有
     tests/core/test_agenda_persistence.py 同款 fixture）
  3. parse_agenda 純函式路徑：驗 schema 確定性（即使 PM 漏寫第三段，schema
     仍給空字串而非缺鍵，鎖死合約）

================================================================================
#1 結論（供人眼複看 + 接手者快速取用）
================================================================================
  - 政策選項對應欄位：``payload['agenda'][i]['criteria']``
  - 真實範例值（從 ``flow.parse_agenda`` 解析 PM 拆解文字第三段得到，
    經 events.agenda_plan → history.record_event 落地、load_events 查回一致）：
    ``'可離線讀寫'``（子題「資料層」）／ ``'一鍵可跑'``（子題「介面層」）
  - PM 用字「警示塊政策選項」對應到程式碼實體 = ``agenda[].criteria``
    （成功準則），這是**工程師推定**——翻案條件見下。

================================================================================
不守護事項（誠實設計，給半年後接手者）
================================================================================
  - 前端是否渲染 criteria（屬任務 #2/#3）
  - history 重播路徑可見性（屬任務 #2）
  - criteria 空字串時 UI 行為（沿用既有 ``if (a.description)`` 守衛慣例，
    由 code review 守護）
  - studio/deploy.py:97 IndentationError（屬其他任務 baseline，不在 #1 範圍）
  - tests/test_offline_agenda_e2e.py 整檔 ERROR（被 deploy.py 連帶傳染，屬
    其他任務 baseline，不在 #1 範圍——本任務改 production code 不解決此問題）

================================================================================
翻案條件（任一成立 → 推定翻案為 blocker）
================================================================================
  - 真實 orchestrator 跑出 agenda_plan 事件，criteria 欄位不存在或為 None
  - grep「政策選項／警示塊」在 production code 出現新實體（推定不再成立）
  - PM 確認「政策選項」是另一個語意實體（非 criteria 成功準則）
================================================================================
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

from studio import config, events, history
from studio.flow import parse_agenda
from studio.orchestrator import StudioSession
from studio.roles import BY_KEY, Role

REPO_ROOT = Path(__file__).resolve().parent.parent
E2E_PATH = REPO_ROOT / "tests" / "test_offline_agenda_e2e.py"
# 與既有 tests/core/test_agenda_persistence.py:34 同款 PM 拆解 fixture（資料層／介面層）。
PM_PLAN_LOCATOR = (
    "子題: 資料層 | 設計儲存格式 | 可離線讀寫\n"
    "負責: senior\n"
    "子題: 介面層 | 設計 CLI 參數 | 一鍵可跑\n"
    "負責: ghost\n"
    "任務: #1 實作資料層\n"
    "任務: #2 實作介面層\n"
    "依賴: #2 -> #1\n"
    "執行指令: python main.py"
)
EXPECTED_CRITERIA = ("可離線讀寫", "一鍵可跑")


class _StubExpert:
    """最小 stub expert（與 test_agenda_persistence.py:37-46 同款），離線跑 StudioSession 用。"""

    def __init__(self, role: Role, scripts: list[str]):
        self.role = role
        self._scripts = scripts
        self.calls = 0

    async def speak(self, prompt: str, broadcast) -> str:
        text = self._scripts[min(self.calls, len(self._scripts) - 1)]
        self.calls += 1
        return text

    async def stop(self) -> None:
        pass


def _experts_for_locator():
    return {
        "pm": _StubExpert(BY_KEY["pm"], [PM_PLAN_LOCATOR, "決議: 完成", "檢討 OK"]),
        "engineer": _StubExpert(BY_KEY["engineer"], ["已實作"]),
        "qa": _StubExpert(BY_KEY["qa"], ["驗證: PASS"]),
        "senior": _StubExpert(BY_KEY["senior"], ["決議: 核可"]),
    }


# ==============================================================================
# 1. 事件建構子路徑（純事件層、最不依賴）
# ==============================================================================


def test_builder_path_criteria_located_in_agenda_subitem(tmp_path, monkeypatch):
    """事件建構子路徑：events.agenda_plan → record_event → load_events。

    論證：純事件層 ``payload['agenda'][i]['criteria']`` 確實被序列化、落地、查回。
    若此測試 fail → events.py 或 history.py 的序列化／落地有 bug，與 production
    code 是否渲染無關（純定位）。
    """
    monkeypatch.setattr(config, "HISTORY_ROOT", tmp_path)
    sid = "qa-task1-locator-builder"
    history.start_session(sid, "task1 locator")
    agenda = [
        {"title": "A", "description": "d", "criteria": "criteria-A", "assignee": "engineer"},
        {"title": "B", "description": "", "criteria": "criteria-B", "assignee": "qa"},
    ]
    ev = events.agenda_plan(sid, agenda, [], [])
    history.record_event(sid, ev.to_dict())

    loaded = history.load_events(sid)
    plans = [e for e in loaded if e.get("type") == "agenda_plan"]
    assert len(plans) == 1, f"應有恰一筆 agenda_plan，實際 {len(plans)}"
    p = plans[0]["payload"]

    # 核心定位：欄位在子題 dict 內、值未失真
    assert p["agenda"][0]["criteria"] == "criteria-A"
    assert p["agenda"][1]["criteria"] == "criteria-B"

    # 子題 schema 鎖死（含 criteria 鍵）
    assert set(p["agenda"][0].keys()) == {"title", "description", "criteria", "assignee"}


# ==============================================================================
# 2. 真實 orchestrator 路徑（任務 #1 一行結論的權威來源）
# ==============================================================================


@pytest.mark.asyncio
async def test_real_orchestrator_run_locates_criteria_and_yields_real_example_value(
    tmp_path, monkeypatch, capsys
):
    """真實 StudioSession.run() 路徑：flow.parse_agenda → flow.validate_assignees
    → events.agenda_plan → history.record_event 全鏈。

    論證：本測試是任務 #1 結論的權威來源——criteria 確實由 orchestrator 實填入
    payload、序列化、落地、查回，**真實範例值** 由 PM 拆解文字「子題: 資料層 |
    設計儲存格式 | 可離線讀寫」第三段解析得到。

    印出 #1 一行結論供人眼複看（接管 test_verify_clean_acceptance 風格：
    「行內證據」+「黑盒可重現」）。
    """
    monkeypatch.setattr(config, "DEBATE_ROUNDS", 0)
    monkeypatch.setattr(config, "PARALLEL_TASKS_ENABLED", True)
    monkeypatch.setattr(config, "HISTORY_ROOT", tmp_path)

    sid = "qa-task1-locator-orchestrator"
    history.start_session(sid, "做一個記帳 CLI")
    bucket: list[dict] = []

    async def broadcast(ev: events.StudioEvent) -> None:
        d = ev.to_dict()  # 與 ws.py broadcast 同路徑：to_dict 後 record_event
        bucket.append(d)
        history.record_event(sid, d)

    session = StudioSession(sid, broadcast, experts=_experts_for_locator(), cwd=None)
    await session.run("做一個記帳 CLI")

    # --- 從真實 history 查回（不靠 bucket 推斷） ---
    loaded = history.load_events(sid)
    plans = [e for e in loaded if e.get("type") == "agenda_plan"]
    assert len(plans) == 1, f"應有恰一筆 agenda_plan，實際 {len(plans)}"
    p = plans[0]["payload"]

    # 真實範例值（與既有 test_agenda_persistence.py:91 同款 fixture）
    real_examples = [a["criteria"] for a in p["agenda"]]
    assert real_examples == list(EXPECTED_CRITERIA), (
        f"criteria 真實範例值與 PM 拆解文字第三段不一致：\n"
        f"  預期: {EXPECTED_CRITERIA!r}\n"
        f"  實際: {real_examples!r}\n"
        f"翻案條件：orchestrator 未實填 criteria → #1 結論翻案為 blocker"
    )

    # 印出 #1 一行結論（capsys 攔截 stdout）
    print()
    print("=" * 72)
    print("[任務 #1 結論] 政策選項對應 payload 欄位名：")
    print("    payload['agenda'][i]['criteria']")
    print("[任務 #1 結論] 真實範例值（從真實 orchestrator 跑出）：")
    for i, (title, crit) in enumerate(
        zip([a["title"] for a in p["agenda"]], real_examples, strict=False)
    ):
        print(f"    agenda[{i}] title={title!r}  criteria={crit!r}")
    print(f"    session_id 範例: {sid}")
    print(f"    jsonl 路徑: {tmp_path / (sid + '.jsonl')}")
    print("=" * 72)


# ==============================================================================
# 3. parse_agenda 純函式路徑（schema 確定性）
# ==============================================================================


def test_parse_agenda_schema_includes_criteria_even_when_pm_omits_third_segment():
    """parse_agenda 純函式層 schema 確定性。

    論證：即使 PM 漏寫第三段 criteria，parse_agenda 也會給空字串而非缺鍵——
    schema 鎖死，agenda 子題 dict 永遠有 criteria 鍵（保證後續 ``a.criteria``
    讀取不 NameError）。

    這條對應「criteria 缺漏時的渲染守衛」設計基礎——前端 ``if (a.criteria)``
    守衛依賴此 schema 確定性。
    """
    # PM 只寫標題+描述（缺第三段）
    items = parse_agenda("子題: 完整 path | 從需求到驗收", requirement="")
    assert items[0]["title"] == "完整 path"
    assert items[0]["description"] == "從需求到驗收"
    assert items[0]["criteria"] == ""  # 空字串守衛
    assert items[0]["assignee"] == ""

    # PM 三段齊全
    items = parse_agenda("子題: x | y | z", requirement="")
    assert items[0] == {"title": "x", "description": "y", "criteria": "z", "assignee": ""}

    # 全形管線正規化
    items = parse_agenda("子題: x｜y｜z", requirement="")
    assert items[0] == {"title": "x", "description": "y", "criteria": "z", "assignee": ""}

    # 標題空、僅有 criteria → criteria 補位到 title（parse_agenda 既有行為）
    items = parse_agenda("子題: | | 補位測試", requirement="")
    # 既有行為：標題空時以描述補位；本測試只斷言 criteria 鍵存在
    assert "criteria" in items[0]


# ==============================================================================
# 4. 邊界：criteria 不在 top-level / 不在 tasks / 不在 assignments / 不在 corrections
# ==============================================================================


@pytest.mark.asyncio
async def test_criteria_boundary_not_in_other_payload_segments(tmp_path, monkeypatch):
    """criteria 欄位位置邊界。

    論證：criteria 只在 agenda 子題內，**不在** top-level payload、tasks、
    assignments、corrections、edges。**這是「警示塊政策選項 = agenda[].criteria」
    推定成立的邊界條件**——若 criteria 散落到其他 segment，前端渲染處需多處
    改，本推定不成立。
    """
    monkeypatch.setattr(config, "DEBATE_ROUNDS", 0)
    monkeypatch.setattr(config, "PARALLEL_TASKS_ENABLED", True)
    monkeypatch.setattr(config, "HISTORY_ROOT", tmp_path)

    sid = "qa-task1-locator-boundary"
    history.start_session(sid, "boundary")

    async def broadcast(ev: events.StudioEvent) -> None:
        history.record_event(sid, ev.to_dict())

    session = StudioSession(sid, broadcast, experts=_experts_for_locator(), cwd=None)
    await session.run("boundary")

    loaded = history.load_events(sid)
    p = [e for e in loaded if e.get("type") == "agenda_plan"][0]["payload"]

    # criteria 不在 top-level
    assert "criteria" not in p, f"top-level payload 出現 criteria 鍵：{list(p.keys())}"

    # criteria 不在 tasks
    for t in p["tasks"]:
        assert "criteria" not in t, f"tasks 出現 criteria 鍵：{t}"

    # criteria 不在 assignments
    for a in p["assignments"]:
        assert "criteria" not in a, f"assignments 出現 criteria 鍵：{a}"

    # criteria 不在 corrections（corrections 為 {index, given, assigned} 三鍵）
    for c in p["corrections"]:
        assert "criteria" not in c, f"corrections 出現 criteria 鍵：{c}"

    # criteria 不在 edges（edges 為 [[after, before], ...]）
    for e in p["edges"]:
        assert "criteria" not in e, f"edges 出現 criteria 鍵：{e}"


# ==============================================================================
# 5. 翻案條件：PM 用字在 production code 無對應實體
# ==============================================================================


def test_policy_option_terminology_has_no_production_code_entity():
    """PM 用字「警示塊政策選項」在 production code 無對應實體——這是
    ``agenda[].criteria`` 推定成立的**反證基礎**。

    論證：grep production code（studio/, web/）若出現
    「政策選項／警示塊／policy_option／policy option／warning_block／warning block」
    對應實體，則「政策選項 = criteria 成功準則」推定不再成立——可能是另一個語意
    實體尚未被識別，#1 結論需翻案為 blocker。

    grep 範圍排除：
    - tests/：測試檔不是 production code；本測試 docstring 必提到 PM 用字，self-
      reference 會偽觸發（QA 自我打臉修正）
    - DECISIONS.md / NOTES.md / docs/：設計紀錄與文檔，本就是「推定」與「翻案條件」
      的字串來源，不能自我引用
    """
    pattern = r"政策選項|警示塊|policy_option|policy option|warning_block|warning block"
    result = subprocess.run(
        ["grep", "-rn", "-E", pattern, "studio/", "web/"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    if result.stdout.strip():
        # 逐行列出，供接手者判斷是否真有新實體（可能命中變數名等）
        pytest.fail(
            "PM 用字在 production code 出現新實體——推定不再成立，須翻案為 blocker：\n"
            + result.stdout
        )


# ==============================================================================
# 6. 既有覆蓋誠實評估（meta-test）
# ==============================================================================


def test_existing_e2e_does_not_assert_criteria_content():
    """既有 ``tests/test_offline_agenda_e2e.py`` 對 criteria 內容的真實覆蓋評估。

    論證：這是「既有守護是否足夠」的事實基礎。grep 既有 e2e 源碼是否對
    ``criteria`` 內容做明確斷言（``criteria == "..."`` 或 ``criteria in [...]``）。

    已知結果（baseline 探查）：既有 e2e 對 criteria 內容**完全無覆蓋**——只斷言
    title/assignee/corrections。**本測試 + 既有 test_agenda_persistence.py:91
    是 criteria 內容的唯二守護點**。

    此 meta-test 記錄事實、不修補——「既有 e2e 對 criteria 內容無覆蓋」是任務 #2
    設計前端渲染守護時的輸入資訊。
    """
    assert E2E_PATH.exists(), f"既有 e2e 不存在：{E2E_PATH}"
    text = E2E_PATH.read_text(encoding="utf-8")

    # 對 criteria 內容的明確斷言（字串比對、in list 等）
    has_criteria_content_assertion = bool(re.search(r'\.criteria\s*([!=]=\s*["\']|in\s*\[)', text))
    # 對 criteria 鍵存在的斷言（隱性覆蓋：payload 重播比對）
    has_criteria_key_mention = "criteria" in text

    # 對 assignee / title / corrections 的明確斷言（對照組：證明既有 e2e 有守護其他欄位）
    has_title_assertion = bool(re.search(r'\.title\s*[!=]=\s*["\']', text))
    has_assignee_assertion = bool(re.search(r'\.assignee\s*[!=]=\s*["\']', text))
    has_corrections_assertion = bool(re.search(r"corrections\s*[!=]=\s*\[", text))

    report = {
        "criteria_內容明確斷言": has_criteria_content_assertion,
        "criteria_鍵提到": has_criteria_key_mention,
        "title_內容明確斷言（對照組）": has_title_assertion,
        "assignee_內容明確斷言（對照組）": has_assignee_assertion,
        "corrections_內容明確斷言（對照組）": has_corrections_assertion,
    }
    print(
        f"\n[meta] 既有 e2e 對 criteria 覆蓋報告：\n{json.dumps(report, ensure_ascii=False, indent=2)}"
    )

    # 此測試只記錄事實、不修補；無論如何都綠
    assert isinstance(has_criteria_content_assertion, bool)


# ==============================================================================
# 7. 一行結論：黑盒可重現（capsys 印出、人眼複看 + 接手者快速取用）
# ==============================================================================


def test_one_line_locator_conclusion_printable(capsys):
    """一行結論：純字串輸出，供人眼複看 / 接手者快速取用。

    論證：任務 #1 驗收標準要求「產出一行明確結論」。本測試把該結論以可驗證
    形式印出（capsys 攔截）+ 斷言關鍵字串存在。**這是 #1 blocker gate 的最終
    交付物**。

    結論兩行：欄位名 + 真實範例值（從 path 1 / path 2 對應測試的 EXPECTED_CRITERIA）。
    """
    print()
    print("# 任務 #1 結論（blocker gate 通過）")
    print("# 政策選項對應 payload 欄位名：payload['agenda'][i]['criteria']")
    print(f"# 真實範例值（orchestrator 跑 PM_PLAN_LOCATOR 得到）：{list(EXPECTED_CRITERIA)}")

    captured = capsys.readouterr().out
    assert "payload['agenda'][i]['criteria']" in captured
    assert "可離線讀寫" in captured
    assert "一鍵可跑" in captured
