"""額度感知 per-task 動態派工（orchestrator 接線）的離線測試。

沿用 tests/core/test_workflow_dynamic.py 的 StubExpert／_session 範式，以 _dispatch_factory
注入 stub（鏡射 _recruit_factory 注入縫）：驗證任務開工前換綁 provider/model、
dispatch_decision 廣播、任務結束（成功/失敗皆）還原原專家並 best-effort stop 臨時專家、
安全護欄（注入 experts 未注入 factory 不換綁、額度全掛不換綁）。全程 stub、不打 LLM。
"""

from __future__ import annotations

import pytest

from studio import config, events, lessons, provider_quota
from studio.orchestrator import LaneContext, StudioSession
from studio.roles import BY_KEY, Role


class StubExpert:
    """依序回傳腳本化回應，記錄呼叫次數／prompt／是否被 stop。"""

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


def _session(pm_scripts=None, qa_scripts=None, senior_scripts=None):
    bucket: list[events.StudioEvent] = []

    async def broadcast(ev):
        bucket.append(ev)

    experts = {
        "pm": StubExpert(BY_KEY["pm"], pm_scripts or ["任務: 做點事"]),
        "engineer": StubExpert(BY_KEY["engineer"], ["工程師已實作"]),
        "qa": StubExpert(BY_KEY["qa"], qa_scripts or ["驗證: PASS"]),
        "senior": StubExpert(BY_KEY["senior"], senior_scripts or ["決議: 核可"]),
    }
    s = StudioSession("t", broadcast, experts=experts, cwd=None)
    s._main_ctx = LaneContext("main", None, experts, None)
    s._requirement = "做一個小工具"
    return s, experts, bucket


def _stub_snapshot():
    """合成快照：claude 30%、codex 5% 皆就緒；minimax/antigravity 未就緒。"""
    return {
        "ok": True,
        "updated_at": 1000.0,
        "providers": [
            {
                "key": "claude",
                "ready": True,
                "rate_limits": {"five_hour": {"used_percentage": 30, "reset_at": None}},
            },
            {
                "key": "codex",
                "ready": True,
                "rate_limits": {"five_hour": {"used_percentage": 5, "reset_at": None}},
            },
            {"key": "minimax", "ready": False, "rate_limits": None},
            {"key": "antigravity", "ready": False, "rate_limits": None},
        ],
    }


def _recording_factory(store: list):
    """鏡射 providers.make_expert 簽名的 stub 工廠，記錄每次換綁的參數。"""

    def factory(role, session_id, cwd, *, provider=None, model=None):
        e = StubExpert(role, ["臨時專家已實作\n決議: 完成"])
        store.append(
            {
                "expert": e,
                "role": role.key,
                "session_id": session_id,
                "provider": provider,
                "model": model,
            }
        )
        return e

    return factory


def _dispatch_events(bucket):
    return [e for e in bucket if e.type is events.EventType.DISPATCH_DECISION]


# --- 換綁 + 廣播 + 還原 -------------------------------------------------------


@pytest.mark.asyncio
async def test_work_task_rebinds_provider_and_model(monkeypatch):
    monkeypatch.setattr(provider_quota, "snapshot", _stub_snapshot)
    s, experts, bucket = _session()
    recruited: list = []
    s._dispatch_factory = _recording_factory(recruited)
    # PM 派工 hint：#1 → codex + 白名單內模型（settings.CODEX_MODELS 含 gpt-5.5）。
    s._dispatch_hints = {1: {"provider": "codex", "model": "gpt-5.5"}}
    task = {"id": 1, "title": "甲", "status": "todo"}
    ok = await s._work_task(s._main_ctx, task, "整體計畫")
    assert ok is True
    # 換綁：臨時專家以 codex/gpt-5.5 建立、session_id 帶 task 標記，實作由它發言。
    assert len(recruited) == 1
    rec = recruited[0]
    assert rec["role"] == "engineer"
    assert rec["provider"] == "codex" and rec["model"] == "gpt-5.5"
    assert rec["session_id"] == "t:task1"
    assert rec["expert"].calls >= 1
    assert experts["engineer"].calls == 0  # 原 engineer 本任務未發言
    # 任務結束：還原原專家、清掉暫時綁定、best-effort stop 臨時專家。
    assert s._main_ctx.experts["engineer"] is experts["engineer"]
    assert "engineer" not in s._dispatch_bindings
    assert rec["expert"].stopped is True
    # 派工序列記錄（後續任務同分時避開剛用過的）。
    assert s._dispatch_recent == ["codex"]
    # dispatch_decision 廣播（前端 log-line／history 重播）。
    evs = _dispatch_events(bucket)
    assert len(evs) == 1
    p = evs[0].payload
    assert p["task_id"] == 1 and p["title"] == "甲" and p["role"] == "engineer"
    assert p["provider"] == "codex" and p["model"] == "gpt-5.5"
    assert p["reason"]  # 繁中一句話決策理由


@pytest.mark.asyncio
async def test_work_task_restores_even_when_task_fails(monkeypatch):
    monkeypatch.setattr(provider_quota, "snapshot", _stub_snapshot)
    monkeypatch.setattr(config, "TASK_MAX_ROUNDS", 1)
    s, experts, bucket = _session(qa_scripts=["驗證: FAIL"], senior_scripts=["決議: 退回"])
    recruited: list = []
    s._dispatch_factory = _recording_factory(recruited)
    s._dispatch_hints = {1: {"provider": "codex", "model": ""}}
    ok = await s._work_task(s._main_ctx, {"id": 1, "title": "甲", "status": "todo"}, "計畫")
    assert ok is False
    assert s._main_ctx.experts["engineer"] is experts["engineer"]  # 失敗路徑也還原
    assert recruited[0]["expert"].stopped is True
    assert "engineer" not in s._dispatch_bindings


@pytest.mark.asyncio
async def test_work_task_model_only_override_same_provider(monkeypatch):
    """provider 與現綁定相同但 PM 指定白名單模型 → 仍換綁以套用模型。"""
    monkeypatch.setattr(provider_quota, "snapshot", _stub_snapshot)
    s, experts, bucket = _session()
    recruited: list = []
    s._dispatch_factory = _recording_factory(recruited)
    # 現綁定＝claude（全域預設）；hint 指同家但指定模型。
    s._dispatch_hints = {1: {"provider": "claude", "model": "claude-haiku-4-5"}}
    ok = await s._work_task(s._main_ctx, {"id": 1, "title": "甲", "status": "todo"}, "計畫")
    assert ok is True
    assert recruited and recruited[0]["provider"] == "claude"
    assert recruited[0]["model"] == "claude-haiku-4-5"


# --- 不換綁的安全路徑 ----------------------------------------------------------


@pytest.mark.asyncio
async def test_work_task_no_rebind_when_choice_matches_current(monkeypatch):
    """自動分派選到與現綁定同家（無模型覆寫）→ 零成本路徑：不建臨時專家、不廣播。"""

    def snap():
        # claude 最低用量 → choose=claude；現綁定亦 claude。
        return {
            "ok": True,
            "updated_at": 1000.0,
            "providers": [
                {
                    "key": "claude",
                    "ready": True,
                    "rate_limits": {"five_hour": {"used_percentage": 5, "reset_at": None}},
                },
                {
                    "key": "codex",
                    "ready": True,
                    "rate_limits": {"five_hour": {"used_percentage": 60, "reset_at": None}},
                },
            ],
        }

    monkeypatch.setattr(provider_quota, "snapshot", snap)
    s, experts, bucket = _session()
    recruited: list = []
    s._dispatch_factory = _recording_factory(recruited)
    ok = await s._work_task(s._main_ctx, {"id": 1, "title": "甲", "status": "todo"}, "計畫")
    assert ok is True
    assert recruited == []  # 未換綁
    assert experts["engineer"].calls >= 1  # 原 engineer 照常實作
    assert _dispatch_events(bucket) == []


@pytest.mark.asyncio
async def test_work_task_no_rebind_when_all_providers_down(monkeypatch):
    def snap():
        return {
            "ok": True,
            "updated_at": 1000.0,
            "providers": [
                {"key": "claude", "ready": False, "rate_limits": None},
                {"key": "codex", "ready": True, "rate_limits": {"error": "unauthorized"}},
            ],
        }

    monkeypatch.setattr(provider_quota, "snapshot", snap)
    s, experts, bucket = _session()
    recruited: list = []
    s._dispatch_factory = _recording_factory(recruited)
    s._dispatch_hints = {1: {"provider": "codex", "model": ""}}
    ok = await s._work_task(s._main_ctx, {"id": 1, "title": "甲", "status": "todo"}, "計畫")
    assert ok is True
    assert recruited == [] and _dispatch_events(bucket) == []  # 全掛 → 沿用原綁定
    assert experts["engineer"].calls >= 1


@pytest.mark.asyncio
async def test_injected_experts_without_factory_never_rebind(monkeypatch):
    """護欄：顯式注入 experts 且未注入 _dispatch_factory → 絕不把 stub 換成真 provider 專家。"""
    monkeypatch.setattr(provider_quota, "snapshot", _stub_snapshot)
    s, experts, bucket = _session()
    assert s._dispatch_factory is None  # 未注入工廠
    s._dispatch_hints = {1: {"provider": "codex", "model": "gpt-5.5"}}
    ok = await s._work_task(s._main_ctx, {"id": 1, "title": "甲", "status": "todo"}, "計畫")
    assert ok is True
    assert experts["engineer"].calls >= 1  # 原 stub 實作
    assert _dispatch_events(bucket) == []
    assert s._main_ctx.experts["engineer"] is experts["engineer"]


# --- 拆解階段接線（派工格式說明 + hint 解析） -----------------------------------


@pytest.mark.asyncio
async def test_stage_decompose_parses_dispatch_hints(monkeypatch):
    monkeypatch.setattr(provider_quota, "snapshot", _stub_snapshot)
    pm_plan = "任務: #1 甲\n任務: #2 乙\n派工: #1 codex gpt-5.5\n派工: #2 claude"
    s, experts, _ = _session(pm_scripts=[pm_plan])
    await s._stage_decompose({"type": "decompose"})
    assert s._dispatch_hints == {
        1: {"provider": "codex", "model": "gpt-5.5"},
        2: {"provider": "claude", "model": ""},
    }
    # PM 拆解 prompt 含派工格式說明與即時額度摘要（依額度把任務分散到各 provider）。
    prompt = experts["pm"].prompts[0]
    assert "派工: #<id> <provider> [<model>]" in prompt
    assert "用量 30%" in prompt


@pytest.mark.asyncio
async def test_stage_decompose_without_dispatch_lines_keeps_empty_hints(monkeypatch):
    monkeypatch.setattr(provider_quota, "snapshot", _stub_snapshot)
    s, experts, _ = _session(pm_scripts=["任務: #1 甲"])
    await s._stage_decompose({"type": "decompose"})
    assert s._dispatch_hints == {}


# --- auto 派工模式（PM 全權：兩家子集、門檻 95、模型直通） ----------------------


def _auto_snapshot():
    """合成快照：minimax 就緒且用量最低（1%）——驗證 auto 模式仍不選子集外的家。"""
    return {
        "ok": True,
        "updated_at": 1000.0,
        "providers": [
            {
                "key": "claude",
                "ready": True,
                "rate_limits": {"five_hour": {"used_percentage": 30, "reset_at": None}},
            },
            {
                "key": "codex",
                "ready": True,
                "rate_limits": {"five_hour": {"used_percentage": 5, "reset_at": None}},
            },
            {
                "key": "minimax",
                "ready": True,
                "rate_limits": {"five_hour": {"used_percentage": 1, "reset_at": None}},
            },
        ],
    }


@pytest.mark.asyncio
async def test_auto_mode_passes_arbitrary_model_to_factory(monkeypatch):
    """auto 派工：PM 指定的任意模型 ID 直通 factory，不查白名單。"""
    monkeypatch.setattr(provider_quota, "snapshot", _stub_snapshot)
    s, experts, bucket = _session()
    s._dispatch_auto = True
    recruited: list = []
    s._dispatch_factory = _recording_factory(recruited)
    s._dispatch_hints = {1: {"provider": "codex", "model": "gpt-brand-new-6"}}
    ok = await s._work_task(s._main_ctx, {"id": 1, "title": "甲", "status": "todo"}, "計畫")
    assert ok is True
    assert recruited and recruited[0]["provider"] == "codex"
    assert recruited[0]["model"] == "gpt-brand-new-6"
    p = _dispatch_events(bucket)[0].payload
    assert p["mode"] == "auto"


@pytest.mark.asyncio
async def test_auto_mode_never_picks_outside_subset(monkeypatch):
    """auto 派工：即使 minimax 就緒且用量最低，候選也夾在 claude/codex 兩家內。"""
    monkeypatch.setattr(provider_quota, "snapshot", _auto_snapshot)
    s, experts, bucket = _session()
    s._dispatch_auto = True
    recruited: list = []
    s._dispatch_factory = _recording_factory(recruited)
    ok = await s._work_task(s._main_ctx, {"id": 1, "title": "甲", "status": "todo"}, "計畫")
    assert ok is True
    assert recruited and recruited[0]["provider"] == "codex"  # 子集中用量最低，非 minimax


@pytest.mark.asyncio
async def test_auto_mode_adopts_hint_at_92_percent(monkeypatch):
    """auto 派工門檻 95：hint 家 92% 照派（手動模式 90 門檻會改派——權力下放的差異點）。"""

    def snap():
        return {
            "ok": True,
            "updated_at": 1000.0,
            "providers": [
                {
                    "key": "claude",
                    "ready": True,
                    "rate_limits": {"five_hour": {"used_percentage": 5, "reset_at": None}},
                },
                {
                    "key": "codex",
                    "ready": True,
                    "rate_limits": {"five_hour": {"used_percentage": 92, "reset_at": None}},
                },
            ],
        }

    monkeypatch.setattr(provider_quota, "snapshot", snap)
    s, experts, bucket = _session()
    s._dispatch_auto = True
    recruited: list = []
    s._dispatch_factory = _recording_factory(recruited)
    s._dispatch_hints = {1: {"provider": "codex", "model": ""}}
    ok = await s._work_task(s._main_ctx, {"id": 1, "title": "甲", "status": "todo"}, "計畫")
    assert ok is True
    assert recruited and recruited[0]["provider"] == "codex"  # 92% < 95 → 尊重 PM


@pytest.mark.asyncio
async def test_stage_decompose_auto_mode_prompt(monkeypatch):
    """auto 模式拆解 prompt：全權說明、provider 只列兩家、額度摘要不出現子集外的家。"""

    async def no_appraisals(_self):
        return {}

    monkeypatch.setattr(lessons, "context", lambda **_kwargs: "")
    monkeypatch.setattr(StudioSession, "_appraisal_perf", no_appraisals)
    monkeypatch.setattr(provider_quota, "snapshot", _auto_snapshot)
    monkeypatch.setattr(config, "dispatch_auto", lambda: True)
    s, experts, _ = _session(pm_scripts=["任務: #1 甲\n派工: #1 codex gpt-5.5"])
    await s._stage_decompose({"type": "decompose"})
    assert s._dispatch_auto is True
    prompt = experts["pm"].prompts[0]
    assert "auto 派工模式" in prompt and "全權" in prompt
    assert "派工: #<id> <provider> <model>" in prompt
    assert "claude、codex" in prompt
    assert "minimax" not in prompt  # 額度摘要與 provider 清單都不得誘導子集外派工


@pytest.mark.asyncio
async def test_auto_mode_never_rebinds_pm(monkeypatch):
    """auto 派工只換綁實作者——PM 專家不經 per-task 派工，釘選（fable-5）不受 auto 模式影響。"""
    monkeypatch.setattr(provider_quota, "snapshot", _stub_snapshot)
    s, experts, bucket = _session()
    s._dispatch_auto = True
    recruited: list = []
    s._dispatch_factory = _recording_factory(recruited)
    s._dispatch_hints = {1: {"provider": "codex", "model": "gpt-brand-new-6"}}
    ok = await s._work_task(s._main_ctx, {"id": 1, "title": "甲", "status": "todo"}, "計畫")
    assert ok is True
    assert all(r["role"] != "pm" for r in recruited)  # 換綁對象只有實作者
    assert s._main_ctx.experts["pm"] is experts["pm"]  # PM 專家原封不動


@pytest.mark.asyncio
async def test_task_result_model_backfilled_from_expert(monkeypatch):
    """模型可見性：未經派工指定模型時，task_result 的 model 取實作專家的 effective_model()。"""
    monkeypatch.setattr(provider_quota, "snapshot", _stub_snapshot)
    s, experts, bucket = _session()
    experts["engineer"].effective_model = lambda: "claude-fable-5"  # StubExpert 動態掛上
    ok = await s._work_task(s._main_ctx, {"id": 1, "title": "甲", "status": "todo"}, "計畫")
    assert ok is True
    res = [e for e in bucket if e.type is events.EventType.TASK_RESULT]
    assert res and res[0].payload["model"] == "claude-fable-5"


@pytest.mark.asyncio
async def test_dispatch_decision_event_mode_manual(monkeypatch):
    """手動模式的 dispatch_decision 事件帶 mode=manual（向後相容欄位）。"""
    monkeypatch.setattr(provider_quota, "snapshot", _stub_snapshot)
    s, experts, bucket = _session()
    recruited: list = []
    s._dispatch_factory = _recording_factory(recruited)
    s._dispatch_hints = {1: {"provider": "codex", "model": ""}}
    ok = await s._work_task(s._main_ctx, {"id": 1, "title": "甲", "status": "todo"}, "計畫")
    assert ok is True
    assert _dispatch_events(bucket)[0].payload["mode"] == "manual"


@pytest.mark.asyncio
async def test_work_task_restores_even_when_broadcast_cancelled(monkeypatch):
    import asyncio

    monkeypatch.setattr(provider_quota, "snapshot", _stub_snapshot)
    s, experts, bucket = _session()

    original_broadcast = s.broadcast

    async def cancel_on_task_result(ev):
        if ev.type == events.EventType.TASK_RESULT:
            raise asyncio.CancelledError()
        await original_broadcast(ev)

    s.broadcast = cancel_on_task_result

    recruited: list = []
    s._dispatch_factory = _recording_factory(recruited)
    s._dispatch_hints = {1: {"provider": "codex", "model": ""}}

    with pytest.raises(asyncio.CancelledError):
        await s._work_task(s._main_ctx, {"id": 1, "title": "甲", "status": "todo"}, "計畫")

    assert s._main_ctx.experts["engineer"] is experts["engineer"]
    assert recruited[0]["expert"].stopped is True
    assert "engineer" not in s._dispatch_bindings
