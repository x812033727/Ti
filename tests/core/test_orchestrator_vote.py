"""3-AI 表決編排（orchestrator._hold_vote 與 dynamic hook）離線測試。

涵蓋：PM 於 _stage_dynamic 發起 `表決:` → 跨 provider 建一次性投票員（factory 注入）→
廣播 VOTE_RESULT → 結果注入下一 hop 的 PM prompt；可用 provider 不足的降級案；VOTE_MAX
單場上限；TI_VOTE_ENABLED=0 關閉案；_task_dynamic_consult 的同款 hook。全程 stub、零 LLM。
"""

from __future__ import annotations

import logging

import pytest

from studio import config, events, lessons
from studio.orchestrator import LaneContext, StudioSession
from studio.roles import BY_KEY, Role


@pytest.fixture(autouse=True)
def _isolated_lessons_file(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "LESSONS_FILE", tmp_path / "lessons.json")


class StubExpert:
    """依序回傳腳本化回應，記錄呼叫次數、收到的 prompt 與是否被 stop。"""

    def __init__(self, role: Role, scripts: list[str]):
        self.role = role
        self._scripts = scripts
        self.calls = 0
        self.prompts: list[str] = []
        self.stopped = False

    async def speak(self, prompt: str, broadcast) -> str:
        self.prompts.append(prompt)
        text = self._scripts[min(self.calls, len(self._scripts) - 1)]
        self.calls += 1
        await broadcast(
            events.expert_message("t", self.role.key, self.role.name, self.role.avatar, text)
        )
        return text

    async def stop(self) -> None:
        self.stopped = True


def _session(pm_scripts):
    bucket: list[events.StudioEvent] = []

    async def broadcast(ev):
        bucket.append(ev)

    experts = {
        "pm": StubExpert(BY_KEY["pm"], pm_scripts),
        "engineer": StubExpert(BY_KEY["engineer"], ["工程師已處理"]),
        "qa": StubExpert(BY_KEY["qa"], ["QA 已驗證"]),
        "senior": StubExpert(BY_KEY["senior"], ["高工已審查"]),
    }
    s = StudioSession("t", broadcast, experts=experts, cwd=None)
    s._main_ctx = LaneContext("main", None, experts, None)
    s._requirement = "做一個小工具"
    return s, experts, bucket


def _snapshot_two_external():
    """claude（PM）30%、codex 10%、minimax 50% 皆就緒 → 可湊足兩位外部投票員。"""
    return {
        "ok": True,
        "updated_at": 1000.0,
        "providers": [
            {
                "key": "claude",
                "ready": True,
                "rate_limits": {
                    "five_hour": {"used_percentage": 30, "reset_at": None},
                    "error": None,
                },
            },
            {
                "key": "codex",
                "ready": True,
                "rate_limits": {
                    "five_hour": {"used_percentage": 10, "reset_at": None},
                    "error": None,
                },
            },
            {
                "key": "minimax",
                "ready": True,
                "rate_limits": {
                    "five_hour": {"used_percentage": 50, "reset_at": None},
                    "error": None,
                },
            },
            {"key": "antigravity", "ready": False, "rate_limits": None},
        ],
    }


def _snapshot_no_external():
    """外部 provider 不足兩位（codex 受限 95%、其餘未就緒）→ 表決降級。"""
    return {
        "ok": True,
        "updated_at": 1000.0,
        "providers": [
            {
                "key": "claude",
                "ready": True,
                "rate_limits": {
                    "five_hour": {"used_percentage": 30, "reset_at": None},
                    "error": None,
                },
            },
            {
                "key": "codex",
                "ready": True,
                "rate_limits": {
                    "five_hour": {"used_percentage": 95, "reset_at": None},
                    "error": None,
                },
            },
            {"key": "minimax", "ready": False, "rate_limits": None},
            {"key": "antigravity", "ready": False, "rate_limits": None},
        ],
    }


def _vote_factory(store: dict, votes_by_provider: dict):
    """記錄被建立的投票員（provider→StubExpert），依 provider 回傳對應選票腳本。"""

    def factory(role, cwd, provider):
        e = StubExpert(role, [votes_by_provider.get(provider, "投票: 無效選項")])
        store[provider] = e
        return e

    return factory


def _vote_events(bucket):
    return [e for e in bucket if e.type is events.EventType.VOTE_RESULT]


# --- _stage_dynamic hook：完整表決週期 ----------------------------------------


@pytest.mark.asyncio
async def test_dynamic_vote_full_cycle(monkeypatch):
    from studio import provider_quota

    monkeypatch.setattr(provider_quota, "snapshot", _snapshot_two_external)
    # hop0：PM 無法決定、發起表決；接著 PM 投 SQLite；hop1：PM 依表決結果收斂。
    s, experts, bucket = _session(
        ["表決: 儲存方案 | SQLite | JSON", "投票: SQLite", "下一步: 結束"]
    )
    created: dict = {}
    s._vote_factory = _vote_factory(created, {"codex": "投票: JSON", "minimax": "投票: JSON"})
    await s._stage_dynamic({"type": "dynamic", "budget": 5})

    # 兩位「不同 provider」的一次性投票員被建立（排除 PM 的 claude），且不進 roster、必 stop。
    assert set(created) == {"codex", "minimax"}
    assert created["codex"].role.key == "voter_codex"
    assert all(e.stopped for e in created.values())
    assert "voter_codex" not in s._main_ctx.experts
    assert "voter_minimax" not in s._main_ctx.experts

    # VOTE_RESULT 廣播：3 票、多數決 JSON 勝出、非平手非降級。
    votes = _vote_events(bucket)
    assert len(votes) == 1
    p = votes[0].payload
    assert p["topic"] == "儲存方案" and p["options"] == ["SQLite", "JSON"]
    assert p["winner"] == "JSON" and p["tie"] is False and p["degraded"] is False
    assert len(p["ballots"]) == 3
    assert p["ballots"][0] == {"voter": "pm", "provider": "claude", "choice": "SQLite"}
    assert {b["provider"] for b in p["ballots"]} == {"claude", "codex", "minimax"}
    rows = lessons.all_lessons()
    assert len(rows) == 1
    assert rows[0]["text"] == "表決先例: 儲存方案 → JSON"
    assert rows[0]["session_id"] == "t"
    assert rows[0]["requirement"] == "做一個小工具"
    assert rows[0]["scope"] == "global"
    assert rows[0]["source"] == "vote"
    assert rows[0]["use_count"] == 0

    # PM prompt 契約：hop0 含表決提示；投票 prompt 含議題與格式指示；下一 hop 注入表決結果。
    pm = experts["pm"]
    assert pm.calls == 3
    assert "可發起表決" in pm.prompts[0]
    assert "表決議題：儲存方案" in pm.prompts[1] and "投票: <選項原文>" in pm.prompts[1]
    assert "表決結果：JSON" in pm.prompts[2]
    assert s._votes_held == 1


@pytest.mark.asyncio
async def test_dynamic_vote_tie_pm_ballot_decides(monkeypatch):
    from studio import provider_quota

    monkeypatch.setattr(provider_quota, "snapshot", _snapshot_two_external)
    # 三方各投一票（平手）→ PM 票定案、tie=True。
    s, experts, bucket = _session(["表決: 方向 | A | B | C", "投票: A", "下一步: 結束"])
    s._vote_factory = _vote_factory({}, {"codex": "投票: B", "minimax": "投票: C"})
    await s._stage_dynamic({"type": "dynamic", "budget": 5})
    p = _vote_events(bucket)[0].payload
    assert p["winner"] == "A" and p["tie"] is True
    assert "表決結果：A" in experts["pm"].prompts[2]
    assert lessons.all_lessons() == []


# --- 降級：可用外部 provider 不足兩位 ------------------------------------------


@pytest.mark.asyncio
async def test_dynamic_vote_degraded_uses_pm_ballot(monkeypatch):
    from studio import provider_quota

    monkeypatch.setattr(provider_quota, "snapshot", _snapshot_no_external)
    s, experts, bucket = _session(["表決: 方向 | A | B", "投票: B", "下一步: 結束"])
    created: dict = {}
    s._vote_factory = _vote_factory(created, {})
    await s._stage_dynamic({"type": "dynamic", "budget": 5})
    assert created == {}  # 降級：不建任何投票員
    p = _vote_events(bucket)[0].payload
    assert p["degraded"] is True
    assert p["winner"] == "B"  # PM 自己的票
    assert len(p["ballots"]) == 1 and p["ballots"][0]["voter"] == "pm"
    assert "表決結果：B" in experts["pm"].prompts[2]  # 照樣注入下一 hop、不卡死流程
    assert lessons.all_lessons() == []


@pytest.mark.asyncio
async def test_dynamic_vote_degraded_pm_abstains_falls_back_to_first_option(monkeypatch):
    from studio import provider_quota

    monkeypatch.setattr(provider_quota, "snapshot", _snapshot_no_external)
    # PM 投票也沒給合法選票 → 棄權 → 以第一選項兜底，流程照走。
    s, experts, bucket = _session(["表決: 方向 | A | B", "我也不知道", "下一步: 結束"])
    s._vote_factory = _vote_factory({}, {})
    await s._stage_dynamic({"type": "dynamic", "budget": 5})
    p = _vote_events(bucket)[0].payload
    assert p["degraded"] is True and p["winner"] == "A"
    assert lessons.all_lessons() == []


def test_record_vote_lesson_uses_exact_only(monkeypatch):
    calls = []
    s, _, _ = _session(["下一步: 結束"])

    def fake_add_many(texts, **kwargs):
        calls.append((texts, kwargs))
        return 1

    monkeypatch.setattr(lessons, "add_many", fake_add_many)
    s._record_vote_lesson(topic="UI 技術", winner="React", tie=False, degraded=False)

    assert calls == [
        (
            ["表決先例: UI 技術 → React"],
            {
                "session_id": "t",
                "requirement": "做一個小工具",
                "source": "vote",
                "exact_only": True,
            },
        )
    ]


def test_record_vote_lesson_failure_only_logs(monkeypatch, caplog):
    s, _, _ = _session(["下一步: 結束"])

    def boom(*_args, **_kwargs):
        raise RuntimeError("store down")

    monkeypatch.setattr(lessons, "add_many", boom)
    caplog.set_level(logging.WARNING, logger="ti.orchestrator")

    s._record_vote_lesson(topic="儲存方案", winner="JSON", tie=False, degraded=False)

    assert "表決先例入庫失敗（議題：儲存方案）" in caplog.text


# --- VOTE_MAX 上限與 TI_VOTE_ENABLED=0 ----------------------------------------


@pytest.mark.asyncio
async def test_vote_max_cap_ignores_request(monkeypatch):
    from studio import provider_quota

    monkeypatch.setattr(provider_quota, "snapshot", _snapshot_two_external)
    monkeypatch.setattr(config, "VOTE_MAX", 0)
    s, experts, bucket = _session(["表決: 方向 | A | B", "下一步: 結束"])
    created: dict = {}
    s._vote_factory = _vote_factory(created, {})
    await s._stage_dynamic({"type": "dynamic", "budget": 5})
    assert created == {} and _vote_events(bucket) == []  # 表決被忽略
    assert s._votes_held == 0
    assert "可發起表決" not in experts["pm"].prompts[0]  # 達上限→提示行不出現、不誤導 PM
    # 該 hop 落回既有 fallback 路徑（無 `下一步:` → engineer 兜底發言），流程不卡死。
    assert experts["engineer"].calls == 1


@pytest.mark.asyncio
async def test_vote_disabled_ignores_request(monkeypatch):
    from studio import provider_quota

    monkeypatch.setattr(provider_quota, "snapshot", _snapshot_two_external)
    monkeypatch.setattr(config, "VOTE_ENABLED", False)
    s, experts, bucket = _session(["表決: 方向 | A | B", "下一步: 結束"])
    created: dict = {}
    s._vote_factory = _vote_factory(created, {})
    await s._stage_dynamic({"type": "dynamic", "budget": 5})
    assert created == {} and _vote_events(bucket) == []
    assert s._votes_held == 0
    assert "可發起表決" not in experts["pm"].prompts[0]  # 關閉時不提示


# --- 投票員失敗＝棄權（不拖垮流程）---------------------------------------------


@pytest.mark.asyncio
async def test_vote_voter_failure_counts_as_abstain(monkeypatch):
    from studio import provider_quota

    monkeypatch.setattr(provider_quota, "snapshot", _snapshot_two_external)
    s, experts, bucket = _session(["表決: 方向 | A | B", "投票: B", "下一步: 結束"])

    class BoomExpert(StubExpert):
        async def speak(self, prompt, broadcast):
            raise RuntimeError("provider down")

    created: dict = {}

    def factory(role, cwd, provider):
        e = BoomExpert(role, [""]) if provider == "codex" else StubExpert(role, ["投票: A"])
        created[provider] = e
        return e

    s._vote_factory = factory
    await s._stage_dynamic({"type": "dynamic", "budget": 5})
    p = _vote_events(bucket)[0].payload
    by_prov = {b["provider"]: b["choice"] for b in p["ballots"]}
    assert by_prov["codex"] == ""  # 失敗的投票員＝棄權
    assert p["winner"] == "B" and p["tie"] is True  # B(pm) vs A(minimax) 平手 → PM 票定案
    assert created["codex"].stopped  # 失敗仍被 stop 回收


# --- _task_dynamic_consult hook -----------------------------------------------


def _consult_wf(budget=3):
    return {
        "name": "追加把關",
        "stages": [
            {
                "type": "build",
                "task_pipeline": [
                    {"type": "implement", "assignee": "engineer"},
                    {"type": "review", "gate": [{"role": "qa", "verdict": "qa_passed"}]},
                    {"type": "dynamic", "budget": budget, "fallback": "engineer"},
                ],
            }
        ],
    }


@pytest.mark.asyncio
async def test_task_dynamic_consult_vote_hook(monkeypatch):
    from studio import provider_quota

    monkeypatch.setattr(provider_quota, "snapshot", _snapshot_two_external)
    bucket: list[events.StudioEvent] = []

    async def broadcast(ev):
        bucket.append(ev)

    experts = {
        "pm": StubExpert(
            BY_KEY["pm"], ["表決: 放行標準 | 嚴格 | 寬鬆", "投票: 嚴格", "下一步: 結束"]
        ),
        "engineer": StubExpert(BY_KEY["engineer"], ["工程師已處理"]),
        "qa": StubExpert(BY_KEY["qa"], ["QA 已驗證"]),
    }
    s = StudioSession("t", broadcast, experts=experts, cwd=None, workflow=_consult_wf())
    s._requirement = "做一個小工具"
    ctx = LaneContext("main", None, experts, None)
    created: dict = {}
    s._vote_factory = _vote_factory(created, {"codex": "投票: 寬鬆", "minimax": "投票: 寬鬆"})
    blocked, feedback = await s._task_dynamic_consult(
        ctx, {"id": 1, "title": "做個東西", "status": "todo"}, "審查摘要", None, broadcast
    )
    assert (blocked, feedback) == (False, "")
    assert set(created) == {"codex", "minimax"}
    p = _vote_events(bucket)[0].payload
    assert p["winner"] == "寬鬆" and p["degraded"] is False
    # 表決結果注入下一 hop 的 PM prompt（prompts[0]=hop0、[1]=投票、[2]=hop1）。
    pm = experts["pm"]
    assert pm.calls == 3
    assert "可發起表決" in pm.prompts[0]
    assert "表決結果：寬鬆" in pm.prompts[2]
    assert s._votes_held == 1
