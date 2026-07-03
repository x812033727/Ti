"""考核機制（orchestrator 接線）的離線測試。

沿用 tests/core/test_orchestrator_dispatch.py 的 StubExpert／_session 範式，全程 stub、
不打 LLM：驗證 _work_task 收客觀指標（QA 輪數/裁決、高工核可、耗時、實際綁定）、
_wrap_up 的 PM 檢討 `考核:` 行→appraisal.record＋APPRAISAL 廣播、拆解 prompt 附
近期考核摘要、per-task 派工把 {provider: avg_score} 傳入 choose_dispatch，以及
appraisal.summary 失敗時拆解／派工照常不炸（容錯 {}）。
"""

from __future__ import annotations

import pytest

from studio import appraisal, config, events, flow, provider_quota
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


def _appraisal_events(bucket):
    return [e for e in bucket if e.type is events.EventType.APPRAISAL]


RETRO_WITH_APPRAISALS = (
    "檢討：整體順利。\n"
    "考核: claude 4 穩定高質量\n"
    "考核: engineer 5 執行力強\n"
    "考核: codex 9 非法分數應被丟棄\n"
)


# --- _work_task 收客觀指標 -----------------------------------------------------


@pytest.mark.asyncio
async def test_work_task_collects_objective_metrics():
    s, experts, _ = _session()
    ok = await s._work_task(s._main_ctx, {"id": 1, "title": "甲", "status": "todo"}, "計畫")
    assert ok is True
    perf = s._task_perf[1]
    assert perf["qa_rounds"] == 1
    assert perf["qa_passed"] is True
    assert perf["senior_approved"] is True
    assert perf["role"] == "engineer"
    assert perf["provider"]  # 實際綁定（stub 未換綁＝角色有效 provider）
    assert perf["model"] is None  # 未經 per-task 派工指定模型 → 取不到＝None
    assert perf["duration_s"] >= 0.0


@pytest.mark.asyncio
async def test_work_task_metrics_reflect_failed_reviews(monkeypatch):
    monkeypatch.setattr(config, "TASK_MAX_ROUNDS", 2)
    s, experts, _ = _session(qa_scripts=["驗證: FAIL"], senior_scripts=["決議: 退回"])
    ok = await s._work_task(s._main_ctx, {"id": 1, "title": "甲", "status": "todo"}, "計畫")
    assert ok is False
    perf = s._task_perf[1]
    assert perf["qa_rounds"] == 2  # 兩輪都進了驗證
    assert perf["qa_passed"] is False
    assert perf["senior_approved"] is False


# --- _wrap_up：考核行 → record + APPRAISAL 廣播 --------------------------------


@pytest.mark.asyncio
async def test_wrap_up_records_appraisals_and_broadcasts(monkeypatch):
    monkeypatch.setattr(config, "LESSONS_ENABLED", False)
    recorded: list[dict] = []
    monkeypatch.setattr(appraisal, "record", lambda entries: recorded.extend(entries))
    s, experts, bucket = _session(pm_scripts=["決議: 完成", RETRO_WITH_APPRAISALS])

    done = await s._wrap_up(experts["pm"], all_ok=True)

    assert done is True
    # 檢討 prompt 含考核格式指示與本場參與 provider 清單。
    retro_prompt = experts["pm"].prompts[1]
    assert "考核: <provider> <1-5分> <一句評語>" in retro_prompt
    assert "本場參與 provider" in retro_prompt
    # 非法分數（9）被解析層丟棄；合法兩筆入庫。
    assert [(e["provider"], e["role"], e["score"]) for e in recorded] == [
        ("claude", "", 4),
        ("claude", "engineer", 5),  # role 指認 → 換算實際綁定 provider
    ]
    assert all(e["session_id"] == "t" and e["created_at"] for e in recorded)
    # 每筆廣播 APPRAISAL（前端 log-line／history 重播）。
    evs = _appraisal_events(bucket)
    assert [(e.payload["score"], e.payload["comment"]) for e in evs] == [
        (4, "穩定高質量"),
        (5, "執行力強"),
    ]


@pytest.mark.asyncio
async def test_wrap_up_merges_objective_metrics_into_entries(monkeypatch):
    monkeypatch.setattr(config, "LESSONS_ENABLED", False)
    recorded: list[dict] = []
    monkeypatch.setattr(appraisal, "record", lambda entries: recorded.extend(entries))
    s, experts, _ = _session(
        pm_scripts=["決議: 完成", "考核: codex 4 穩\n考核: minimax 3 無任務可佐證"]
    )
    # 模擬本場 #1 任務由 codex/gpt-5.5 實作（per-task 派工換綁後 _collect_task_perf 的暫存形狀）。
    s._task_perf = {
        1: {
            "qa_rounds": 2,
            "qa_passed": True,
            "senior_approved": True,
            "provider": "codex",
            "model": "gpt-5.5",
            "duration_s": 12.5,
            "role": "engineer",
        }
    }

    await s._wrap_up(experts["pm"], all_ok=True)

    by_provider = {e["provider"]: e for e in recorded}
    codex = by_provider["codex"]
    assert codex["task_id"] == 1 and codex["model"] == "gpt-5.5"
    assert codex["objective"] == {
        "qa_rounds": 2,
        "qa_passed": True,
        "senior_approved": True,
        "duration_s": 12.5,
    }
    # 沒做過任務的 provider：客觀欄位全 None、不虛構。
    minimax = by_provider["minimax"]
    assert minimax["task_id"] is None and minimax["model"] == ""
    assert minimax["objective"] == {
        "qa_rounds": None,
        "qa_passed": None,
        "senior_approved": None,
        "duration_s": None,
    }


@pytest.mark.asyncio
async def test_wrap_up_without_appraisal_lines_is_noop(monkeypatch):
    monkeypatch.setattr(config, "LESSONS_ENABLED", False)
    called: list = []
    monkeypatch.setattr(appraisal, "record", lambda entries: called.append(entries))
    s, experts, bucket = _session(pm_scripts=["決議: 完成", "檢討：這次沒有考核行"])
    await s._wrap_up(experts["pm"], all_ok=True)
    assert called == [] and _appraisal_events(bucket) == []


@pytest.mark.asyncio
async def test_wrap_up_record_failure_does_not_break_session(monkeypatch):
    """考核入庫炸掉（磁碟/權限…）→ 只記 log，收尾照常、DONE 照發。"""
    monkeypatch.setattr(config, "LESSONS_ENABLED", False)

    def boom(entries):
        raise RuntimeError("disk full")

    monkeypatch.setattr(appraisal, "record", boom)
    s, experts, bucket = _session(pm_scripts=["決議: 完成", RETRO_WITH_APPRAISALS])
    done = await s._wrap_up(experts["pm"], all_ok=True)
    assert done is True
    assert events.EventType.DONE in {e.type for e in bucket}
    assert _appraisal_events(bucket)  # 廣播不受入庫失敗影響


@pytest.mark.asyncio
async def test_wrap_up_appraisal_disabled(monkeypatch):
    monkeypatch.setattr(config, "LESSONS_ENABLED", False)
    monkeypatch.setattr(config, "APPRAISAL_ENABLED", False)
    called: list = []
    monkeypatch.setattr(appraisal, "record", lambda entries: called.append(entries))
    s, experts, bucket = _session(pm_scripts=["決議: 完成", RETRO_WITH_APPRAISALS])
    await s._wrap_up(experts["pm"], all_ok=True)
    assert "考核" not in experts["pm"].prompts[1]  # 檢討 prompt 不附考核指示
    assert called == [] and _appraisal_events(bucket) == []


# --- 拆解 prompt 附近期考核摘要 -------------------------------------------------


@pytest.mark.asyncio
async def test_stage_decompose_injects_appraisal_note(monkeypatch):
    monkeypatch.setattr(provider_quota, "snapshot", _stub_snapshot)
    monkeypatch.setattr(
        appraisal,
        "summary",
        lambda limit_days=30: {
            "providers": {
                "claude": {"avg_score": 4.5, "n": 12, "pass_rate": 0.92},
                "codex": {"avg_score": 3.8, "n": 5, "pass_rate": None},
            },
            "models": {},
        },
    )
    s, experts, _ = _session(pm_scripts=["任務: #1 甲"])
    await s._stage_decompose({"type": "decompose"})
    prompt = experts["pm"].prompts[0]
    assert "各 AI 近期考核" in prompt
    assert "claude 4.5（12 件，通過率 92%）" in prompt
    assert "codex 3.8（5 件）" in prompt  # pass_rate None → 省略通過率段


@pytest.mark.asyncio
async def test_stage_decompose_summary_failure_tolerated(monkeypatch):
    """appraisal.summary 炸掉 → 容錯 {}：拆解照常、prompt 無考核段。"""
    monkeypatch.setattr(provider_quota, "snapshot", _stub_snapshot)

    def boom(limit_days=30):
        raise RuntimeError("corrupt store")

    monkeypatch.setattr(appraisal, "summary", boom)
    s, experts, _ = _session(pm_scripts=["任務: #1 甲"])
    await s._stage_decompose({"type": "decompose"})  # 不炸
    assert s._tasks and s._tasks[0]["title"] == "甲"
    assert "各 AI 近期考核" not in experts["pm"].prompts[0]


# --- per-task 派工把 performance 傳入 choose_dispatch ---------------------------


def _recording_factory(store: list):
    def factory(role, session_id, cwd, *, provider=None, model=None):
        e = StubExpert(role, ["臨時專家已實作"])
        store.append({"expert": e, "provider": provider, "model": model})
        return e

    return factory


@pytest.mark.asyncio
async def test_dispatch_passes_performance_to_choose_dispatch(monkeypatch):
    monkeypatch.setattr(provider_quota, "snapshot", _stub_snapshot)
    monkeypatch.setattr(
        appraisal,
        "summary",
        lambda limit_days=30: {
            "providers": {
                "claude": {"avg_score": 2.0, "n": 3, "pass_rate": 0.5},
                "codex": {"avg_score": 4.9, "n": 8, "pass_rate": 1.0},
            },
            "models": {},
        },
    )
    seen: list[dict] = []
    real_choose = flow.choose_dispatch

    def spy(digest, task, hint, allowed_models, recent, performance=None, threshold=90.0):
        seen.append(performance)
        return real_choose(digest, task, hint, allowed_models, recent, performance, threshold)

    monkeypatch.setattr(flow, "choose_dispatch", spy)
    s, experts, _ = _session()
    s._dispatch_factory = _recording_factory([])
    await s._work_task(s._main_ctx, {"id": 1, "title": "甲", "status": "todo"}, "計畫")
    assert seen == [{"claude": 2.0, "codex": 4.9}]


@pytest.mark.asyncio
async def test_dispatch_summary_failure_tolerated(monkeypatch):
    """派工點 summary 失敗 → performance={}，任務照常完成、不炸。"""
    monkeypatch.setattr(provider_quota, "snapshot", _stub_snapshot)

    def boom(limit_days=30):
        raise RuntimeError("corrupt store")

    monkeypatch.setattr(appraisal, "summary", boom)
    recruited: list = []
    s, experts, _ = _session()
    s._dispatch_factory = _recording_factory(recruited)
    ok = await s._work_task(s._main_ctx, {"id": 1, "title": "甲", "status": "todo"}, "計畫")
    assert ok is True
    assert recruited and recruited[0]["provider"] == "codex"  # 純額度分派：用量最低者
