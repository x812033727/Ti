"""原生快車道(軌 I):派發決策/prompt 組裝/自我升級 sentinel/整合走閘門尾巴。"""

from __future__ import annotations

import asyncio
import json

import pytest

from studio import autopilot, backlog, config

_AUTOPILOT_REPO = "core/autopilot"


@pytest.fixture(autouse=True)
def _forbid_real_subprocess(monkeypatch):
    async def _boom(*args, **kwargs):
        raise RuntimeError("test forbids spawning a real subprocess")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _boom)


@pytest.fixture(autouse=True)
def _base_config(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(config, "HISTORY_ROOT", tmp_path / "history")
    monkeypatch.setattr(config, "AUTOPILOT_REPO", _AUTOPILOT_REPO)
    monkeypatch.setattr(config, "AUTOPILOT_DRYRUN", False)
    monkeypatch.setattr(config, "AUTOPILOT_DAILY_PR_BUDGET", 0)
    monkeypatch.setattr(config, "FAST_LANE", True)
    monkeypatch.setattr(backlog, "_read_cache", {}, raising=False)


# --- 派發決策 -----------------------------------------------------------------


def test_route_default_fast_and_exceptions(monkeypatch):
    assert autopilot._fast_lane_route({"id": 1}) is True, "預設=快車道(全切)"
    assert autopilot._fast_lane_route({"id": 1, "lane": "full"}) is False, "lane=full 恆完整管線"
    assert autopilot._fast_lane_route({"id": 1, "attempts": 2}) is False, "敗兩次降級完整管線"
    monkeypatch.setattr(config, "FAST_LANE", False)
    assert autopilot._fast_lane_route({"id": 1}) is False, "旗標關=零行為變更"


def test_prompt_assembly(monkeypatch):
    from studio import adr, lessons

    monkeypatch.setattr(autopilot, "north_star_context", lambda: "[北極星]")
    monkeypatch.setattr(lessons, "context", lambda requirement="", **k: "[教訓]")
    monkeypatch.setattr(adr, "context", lambda cwd, limit=None: "[ADR]")
    p = autopilot._fast_lane_prompt("修 X bug", "/tmp/clone")
    for marker in ("[北極星]", "[教訓]", "[ADR]", "修 X bug", "需完整管線", "ti-fast-implement"):
        assert marker in p, marker


def test_fast_lane_prompt_carries_multiline_requirement_as_json_payload(monkeypatch):
    from studio import adr, lessons

    monkeypatch.setattr(autopilot, "north_star_context", lambda: "")
    monkeypatch.setattr(lessons, "context", lambda requirement="", **k: "")
    monkeypatch.setattr(adr, "context", lambda cwd, limit=None: "")
    requirement = "修派工截斷\n1. 保留清單\n```bash\necho ok\n```"
    p = autopilot._fast_lane_prompt(requirement, "/tmp/clone")
    payload = p.split("```json\n", 1)[1].split("\n```", 1)[0]
    assert json.loads(payload) == {"requirement": requirement}


# --- 快車道執行 ----------------------------------------------------------------


class _FakeExpert:
    reply = "完成:已修好並通過自查"

    def __init__(self, role, sid, cwd, **kw):
        pass

    async def speak(self, prompt, broadcast):
        return _FakeExpert.reply

    async def stop(self):
        return None


@pytest.fixture()
def fake_engineer(monkeypatch):
    from studio import providers

    _FakeExpert.reply = "完成:已修好並通過自查"
    monkeypatch.setattr(
        providers, "make_expert", lambda role, sid, cwd, **k: _FakeExpert(role, sid, cwd)
    )
    return _FakeExpert


@pytest.mark.asyncio
async def test_run_fast_lane_completed_and_escalate(fake_engineer):
    async def _noop(_e):
        return None

    r = await autopilot._run_fast_lane({"id": 1}, "/tmp/c", "sid", "req", _noop)
    assert r == {"completed": True}
    fake_engineer.reply = "需完整管線: 跨子系統遷移,需要架構評審"
    r = await autopilot._run_fast_lane({"id": 1}, "/tmp/c", "sid", "req", _noop)
    assert r["fast_escalate"].startswith("跨子系統遷移")


# --- 整合:run_one_task 快車道走閘門尾巴出 audit --------------------------------


def _patch_machinery(monkeypatch, tmp_path):
    async def _fake_clone():
        return str(tmp_path / "clone")

    class _NoSession:
        def __init__(self, *a, **k):
            raise AssertionError("快車道不得建 StudioSession")

    async def _gate_ok(clone):
        return (True, "")

    async def _run(cmd, cwd=None, timeout=600, **kwargs):
        if "rev-parse" in cmd:
            return (0, "abc1234\n")
        return (0, "")

    monkeypatch.setattr(autopilot, "_prepare_clone", _fake_clone)
    monkeypatch.setattr(autopilot, "StudioSession", _NoSession)
    monkeypatch.setattr(autopilot, "_gate_lint", _gate_ok)
    monkeypatch.setattr(autopilot, "_gate_collect_without_sdk", _gate_ok)
    monkeypatch.setattr(autopilot, "_gate_tests", _gate_ok)
    monkeypatch.setattr(autopilot, "_run", _run)

    class _MergeRes(tuple):
        pr_number = 7
        branch = "autopilot/task-x"

    async def _merge_ok(clone, task):
        return _MergeRes((True, "已合併"))

    monkeypatch.setattr(autopilot, "_commit_push_merge", _merge_ok)

    async def _never_idle(timeout=600):
        return False  # 跳過重佈段

    monkeypatch.setattr(autopilot, "_wait_until_idle", _never_idle)


def _audit_lines():
    path = autopilot._audit_path()
    if not path.is_file():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


@pytest.mark.asyncio
async def test_run_one_task_fast_lane_end_to_end(monkeypatch, tmp_path, fake_engineer):
    _patch_machinery(monkeypatch, tmp_path)
    (tmp_path / "clone").mkdir(parents=True, exist_ok=True)
    task = backlog.add("修一個小 bug")
    await autopilot.run_one_task(task)
    rows = _audit_lines()
    assert len(rows) == 1 and rows[0]["outcome"] == "merged"
    assert rows[0]["lane"] == "fast", "audit 帶 lane 供效能對比"
    assert backlog.list_tasks()[0]["status"] == "done"


@pytest.mark.asyncio
async def test_run_one_task_fast_escalate_requeues_full(monkeypatch, tmp_path, fake_engineer):
    _patch_machinery(monkeypatch, tmp_path)
    (tmp_path / "clone").mkdir(parents=True, exist_ok=True)
    fake_engineer.reply = "需完整管線: 動到 orchestrator 核心"
    task = backlog.add("大改架構")
    await autopilot.run_one_task(task)
    t = backlog.list_tasks()[0]
    assert t["status"] == "pending" and t.get("lane") == "full"
    assert t.get("attempts") == 0, "升級不燒 attempts"
    assert "[快車道升級]" in t.get("note", "")
    assert _audit_lines() == [], "升級不落 merge audit"


# --- I3:改良場流程選擇 --------------------------------------------------------


def test_improver_task_workflow_selection(monkeypatch):
    from studio import improver, workflow

    monkeypatch.setattr(config, "FAST_LANE", True)
    wf = improver.ProjectImprover._task_workflow({"id": 1})
    assert wf and wf["name"] == workflow.IMPLEMENT_FAST_NAME
    assert [s["type"] for s in wf["stages"]] == ["decompose", "build", "demo", "wrap_up", "publish"]
    assert improver.ProjectImprover._task_workflow({"id": 1, "lane": "full"}) is None
    monkeypatch.setattr(config, "FAST_LANE", False)
    assert improver.ProjectImprover._task_workflow({"id": 1}) is None


def test_implement_fast_workflow_validates():
    from studio import workflow

    v = workflow.coerce(workflow.implement_fast_workflow())
    assert v["name"] == workflow.IMPLEMENT_FAST_NAME, "coerce 不得退回 default"
    assert workflow.IMPLEMENT_FAST_NAME in workflow._BUILTIN_WORKFLOWS


# --- 軌 K:FAST 快審顧問 --------------------------------------------------------


class _AdvisorEnv:
    def __init__(self, monkeypatch, diff="+ x = 1", advice="OK"):
        from studio import providers

        self.speaks: list[str] = []
        env = self

        class _Ex:
            def __init__(self, *a, **k):
                pass

            async def speak(self, prompt, broadcast):
                env.speaks.append(prompt)
                return "完成"

            async def stop(self):
                return None

        async def _run(cmd, cwd=None, timeout=600, **kw):
            if cmd[:2] == ["git", "diff"]:
                return (0, diff)
            return (0, "")

        async def _advise(system, user, *, session_id, cwd, **kw):
            env.advise_called = True
            return advice

        self.advise_called = False
        monkeypatch.setattr(providers, "make_expert", lambda role, sid, cwd, **k: _Ex())
        monkeypatch.setattr(providers, "complete_once", _advise)
        monkeypatch.setattr(autopilot, "_run", _run)
        monkeypatch.setattr(config, "FAST_ADVISOR", True)


async def _noop_bc(_e):
    return None


@pytest.mark.asyncio
async def test_advisor_ok_no_second_round(monkeypatch):
    env = _AdvisorEnv(monkeypatch, advice="OK")
    r = await autopilot._run_fast_lane({"id": 1}, "/tmp/c", "sid", "req", _noop_bc)
    assert r == {"completed": True} and env.advise_called
    assert len(env.speaks) == 1, "顧問放行=不追加回修輪"


@pytest.mark.asyncio
async def test_advisor_tips_trigger_one_revision(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "AUTOPILOT_STATE_DIR", tmp_path / "ap")
    monkeypatch.setattr(backlog, "_read_cache", {}, raising=False)
    t = backlog.add("x")
    env = _AdvisorEnv(monkeypatch, advice="建議: config 少了 reload 區塊\n建議: .env.example 沒補")
    r = await autopilot._run_fast_lane({"id": t["id"]}, "/tmp/c", "sid", "req", _noop_bc)
    assert r == {"completed": True}
    assert len(env.speaks) == 2, "有建議=恰好一輪回修"
    assert "config 少了 reload 區塊" in env.speaks[1]
    assert "[advisor]" in (backlog.list_tasks()[0].get("note") or "")


@pytest.mark.asyncio
async def test_advisor_flag_off_and_empty_diff(monkeypatch):
    env = _AdvisorEnv(monkeypatch, advice="建議: 假建議")
    monkeypatch.setattr(config, "FAST_ADVISOR", False)
    await autopilot._run_fast_lane({"id": 1}, "/tmp/c", "sid", "req", _noop_bc)
    assert env.advise_called is False, "旗標關=零顧問成本"
    env2 = _AdvisorEnv(monkeypatch, diff="", advice="建議: 假建議")
    await autopilot._run_fast_lane({"id": 1}, "/tmp/c", "sid", "req", _noop_bc)
    assert env2.advise_called is False, "空 diff=跳過顧問"
