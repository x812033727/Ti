"""架構決策記錄（ADR）：模組單元測試 + orchestrator 掛點測試（stub 專家，不需 LLM）。"""

from __future__ import annotations

import pytest

from studio import adr, config, events
from studio.orchestrator import StudioSession
from studio.roles import BY_KEY, Role

THREE_LINE = """前置說明應被忽略。
決策: 後端用 FastAPI
理由: 團隊熟悉、生態完整
否決: Flask（缺原生 async）
決策: 前端免建置，直接 HTML/JS
"""

LEGACY = """設計決策: 資料存 SQLite
設計決策: 用 WebSocket 推播
"""


@pytest.fixture
def cwd(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ADR_ENABLED", True)
    return tmp_path


def test_parse_three_line_format():
    entries = adr.parse_adr(THREE_LINE)
    assert len(entries) == 2
    assert entries[0]["decision"] == "後端用 FastAPI"
    assert entries[0]["rationale"].startswith("團隊熟悉")
    assert entries[0]["rejected"].startswith("Flask")
    assert entries[1]["rationale"] == "" and entries[1]["rejected"] == ""


def test_parse_legacy_design_decision_lines():
    entries = adr.parse_adr(LEGACY)
    assert [e["decision"] for e in entries] == ["資料存 SQLite", "用 WebSocket 推播"]


def test_parse_no_decision_lines_returns_empty():
    assert adr.parse_adr("自由發揮的討論，沒有格式行") == []
    assert adr.parse_adr("") == []
    assert adr.parse_adr("理由: 孤兒理由行不該產生條目") == []


def test_record_and_files(cwd):
    n = adr.record(cwd, adr.parse_adr(THREE_LINE), session_id="s1")
    assert n == 2
    entries = adr.all_entries(cwd)
    assert entries[0]["session_id"] == "s1" and entries[0]["created_at"] > 0
    md = (cwd / "DECISIONS.md").read_text(encoding="utf-8")
    assert md.startswith("# 架構決策記錄")
    assert "## 後端用 FastAPI" in md and "理由：團隊熟悉" in md and "否決方案：Flask" in md


def test_record_dedup_full_text(cwd):
    adr.record(cwd, adr.parse_adr(THREE_LINE))
    n = adr.record(cwd, adr.parse_adr(THREE_LINE))  # 同決策重提 → 全部去重
    assert n == 0 and len(adr.all_entries(cwd)) == 2


def test_record_none_cwd_and_empty(cwd):
    assert adr.record(None, [{"decision": "x"}]) == 0
    assert adr.record(cwd, []) == 0
    assert adr.record(cwd, [{"decision": "   "}]) == 0
    assert not (cwd / "DECISIONS.md").exists()


def test_context_injection_and_gates(cwd, monkeypatch):
    adr.record(cwd, adr.parse_adr(THREE_LINE))
    ctx = adr.context(cwd)
    assert "【既有架構決策" in ctx and "後端用 FastAPI（理由：" in ctx
    assert adr.context(cwd, limit=1).count("- ") == 1  # limit 取最新 N 筆
    assert adr.context(None) == ""
    monkeypatch.setattr(config, "ADR_ENABLED", False)
    assert adr.context(cwd) == ""  # 開關關閉 → 空字串、零影響


def test_context_empty_without_entries(cwd):
    assert adr.context(cwd) == ""


# --- orchestrator 掛點（stub 專家） ---------------------------------------


class StubExpert:
    """依角色給腳本化回應，記錄收到的 prompt（同 test_orchestrator 慣例）。"""

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


async def _noop_broadcast(ev):
    pass


def _experts(pm, eng, qa, senior, architect=None):
    experts = {
        "pm": StubExpert(BY_KEY["pm"], pm),
        "engineer": StubExpert(BY_KEY["engineer"], eng),
        "qa": StubExpert(BY_KEY["qa"], qa),
        "senior": StubExpert(BY_KEY["senior"], senior),
    }
    if architect is not None:
        experts["architect"] = StubExpert(BY_KEY["architect"], architect)
    return experts


@pytest.fixture
def flow(monkeypatch):
    monkeypatch.setattr(config, "ADR_ENABLED", True)
    monkeypatch.setattr(config, "PARALLEL_TASKS_ENABLED", False)
    monkeypatch.setattr(config, "DEBATE_ROUNDS", 1)
    # 這些掛點測試針對「legacy 兩人辯論 → ADR 蒸餾」的腳本化發言序（senior 第二次發言＝蒸餾）。
    # 預設已是 parallel，engine 路徑的蒸餾接縫另由 test_discussion.py 專測，故此處 pin legacy。
    monkeypatch.setattr(config, "DISCUSS_MODE", "legacy")


async def test_architect_decision_recorded(tmp_path, flow):
    experts = _experts(
        pm=["任務: 實作", "決議: 完成", "檢討"],
        eng=["意見", "實作"],
        qa=["驗證: PASS"],
        senior=["意見", "決議: 核可"],
        architect=["提案", "設計決策: 用 SQLite 存資料\n理由: 零維運"],
    )
    session = StudioSession("t", _noop_broadcast, experts=experts, cwd=tmp_path)
    await session.run("需求")

    entries = adr.all_entries(tmp_path)
    assert [e["decision"] for e in entries] == ["用 SQLite 存資料"]
    assert entries[0]["rationale"] == "零維運"
    assert (tmp_path / "DECISIONS.md").is_file()


async def test_debate_distilled_to_adr(tmp_path, flow):
    # 無架構師 → 辯論路徑：senior 第二次發言是收斂蒸餾，輸出決策行。
    experts = _experts(
        pm=["任務: 實作", "決議: 完成", "檢討"],
        eng=["提案", "實作"],
        qa=["驗證: PASS"],
        senior=["點評", "決策: 單檔 CLI 起步\n理由: 範圍小", "決議: 核可"],
    )
    session = StudioSession("t", _noop_broadcast, experts=experts, cwd=tmp_path)
    await session.run("需求")

    assert [e["decision"] for e in adr.all_entries(tmp_path)] == ["單檔 CLI 起步"]
    assert "蒸餾成決策記錄" in experts["senior"].prompts[1]


async def test_pm_prompt_carries_existing_adr(tmp_path, flow):
    # 既有決策 → 下一場同 workspace 的 PM 拆解 prompt 應帶入 ADR 區塊（翻案須說明理由）。
    adr.record(tmp_path, [{"decision": "後端用 FastAPI"}])
    experts = _experts(
        pm=["任務: 實作", "決議: 完成", "檢討"],
        eng=["提案", "實作"],
        qa=["驗證: PASS"],
        senior=["點評", "沒有新決策", "決議: 核可"],
    )
    session = StudioSession("t", _noop_broadcast, experts=experts, cwd=tmp_path)
    await session.run("需求")

    assert "【既有架構決策" in experts["pm"].prompts[0]
    assert "後端用 FastAPI" in experts["pm"].prompts[0]


async def test_adr_disabled_no_side_effects(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ADR_ENABLED", False)
    monkeypatch.setattr(config, "PARALLEL_TASKS_ENABLED", False)
    monkeypatch.setattr(config, "DEBATE_ROUNDS", 1)
    # legacy 兩人辯論：senior 只有「點評＋審查」兩次（engine 路徑另測），故 pin legacy。
    monkeypatch.setattr(config, "DISCUSS_MODE", "legacy")
    experts = _experts(
        pm=["任務: 實作", "決議: 完成", "檢討"],
        eng=["提案", "實作"],
        qa=["驗證: PASS"],
        senior=["點評", "決議: 核可"],
    )
    session = StudioSession("t", _noop_broadcast, experts=experts, cwd=tmp_path)
    await session.run("需求")

    assert not (tmp_path / "adr.json").exists()
    assert not (tmp_path / "DECISIONS.md").exists()
    # 關閉時辯論不多一次蒸餾發言：senior 只有點評＋審查兩次。
    assert experts["senior"].calls == 2
    assert "【既有架構決策" not in experts["pm"].prompts[0]
