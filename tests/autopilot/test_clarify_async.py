"""async clarify(第 4 階 B4):探測關鍵歧義→parked 等人答;人答/逾時依假設前進。

守護不變量:
- TI_CLARIFY_ASYNC=0(預設)零探測零行為變更;僅首攻且未探測過(無 clarify 欄位)的
  完整管線任務探測。
- 有問題→parked:問題存專用 clarify 欄位(unpark 覆寫 note 也不丟)、note=[待澄清]、
  attempts 回填揀起前值(不耗重試額度)、page 推播 clarify_pending、audit 記 clarify_parked。
- 逾時掃描:只動 note 仍為 [待澄清] 開頭者(人工處置過的不碰);attempts 不動。
- requirement 併入澄清紀錄;[手動] 取回:<回覆> 視為人工回覆。
"""

from __future__ import annotations

import time

import pytest

from studio import autopilot, backlog, config


@pytest.fixture(autouse=True)
def _state(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_STATE_DIR", tmp_path / "ap")
    (tmp_path / "ap").mkdir(parents=True)
    monkeypatch.setattr(backlog, "_read_cache", {}, raising=False)
    monkeypatch.setattr(config, "CLARIFY_ASYNC", True)
    monkeypatch.setattr(config, "CLARIFY_ASYNC_TIMEOUT_H", 24.0)
    monkeypatch.setattr(config, "AUTOPILOT_DRYRUN", True)
    monkeypatch.setattr(autopilot, "_clarify_sweep_at", 0.0)
    return tmp_path


def _fake_probe(monkeypatch, text):
    async def fake(system, user, *, session_id, cwd, timeout=120.0):
        return text

    import studio.providers as providers_mod

    monkeypatch.setattr(providers_mod, "complete_once", fake)


@pytest.mark.asyncio
async def test_probe_parses_questions_and_no_need(monkeypatch):
    _fake_probe(
        monkeypatch, "問題: 要支援哪些幣別?\n假設: 只做 TWD\n問題: 要即時匯率嗎?\n假設: 不要"
    )
    qs = await autopilot._clarify_probe({"title": "x", "detail": ""}, "/tmp", "s1")
    assert [q["q"] for q in qs] == ["要支援哪些幣別?", "要即時匯率嗎?"]
    _fake_probe(monkeypatch, "澄清: 不需要")
    assert await autopilot._clarify_probe({"title": "x"}, "/tmp", "s1") == []


@pytest.mark.asyncio
async def test_run_one_task_parks_with_clarify_field(monkeypatch):
    t = backlog.add("模糊任務")

    async def clone():
        return "/tmp/clone"

    async def no_prefilter(task, clone):
        return None

    async def probe(task, clone, sid):
        return [{"q": "範圍多大?", "assumption": "只動 studio/"}]

    sent = []
    monkeypatch.setattr(autopilot, "_prepare_clone", clone)
    monkeypatch.setattr(autopilot, "_prefilter_implemented_match", no_prefilter)
    monkeypatch.setattr(autopilot, "_is_investigation_task", lambda task: False)
    monkeypatch.setattr(autopilot, "_clarify_probe", probe)
    monkeypatch.setattr(
        autopilot.notify, "send_bg", lambda kind, title, **kw: sent.append((kind, kw))
    )

    await autopilot.run_one_task(dict(t))
    got = backlog.list_tasks()[0]
    assert got["status"] == "parked"
    assert got["note"].startswith("[待澄清]") and "範圍多大?" in got["clarify"]
    assert got["attempts"] == 0, "澄清等待不耗重試額度(回填揀起前值)"
    assert [k for k, _ in sent] == ["clarify_pending"]


@pytest.mark.asyncio
async def test_flag_off_or_already_probed_skips(monkeypatch):
    t = backlog.add("任務")
    probes = {"n": 0}

    async def clone():
        return "/tmp/clone"

    async def no_prefilter(task, clone):
        return None

    async def probe(task, clone, sid):
        probes["n"] += 1
        return [{"q": "?", "assumption": ""}]

    monkeypatch.setattr(autopilot, "_prepare_clone", clone)
    monkeypatch.setattr(autopilot, "_prefilter_implemented_match", no_prefilter)
    monkeypatch.setattr(autopilot, "_is_investigation_task", lambda task: False)
    monkeypatch.setattr(autopilot, "_clarify_probe", probe)

    def stop_here(*a, **k):
        raise RuntimeError("到這代表沒走 clarify 出口")

    monkeypatch.setattr(autopilot.history, "start_session", stop_here)

    monkeypatch.setattr(config, "CLARIFY_ASYNC", False)
    with pytest.raises(RuntimeError):
        await autopilot.run_one_task(dict(t))
    assert probes["n"] == 0, "旗標關=零探測"

    monkeypatch.setattr(config, "CLARIFY_ASYNC", True)
    with pytest.raises(RuntimeError):
        await autopilot.run_one_task({**t, "clarify": "問過了"})
    assert probes["n"] == 0, "已探測過(clarify 欄位在)不重問"


def test_timeout_sweep_resumes_only_untouched(monkeypatch):
    t1 = backlog.add("逾時該復活")
    backlog.set_status(t1["id"], "parked", note="[待澄清] 問:x(假設:y)", clarify="問:x(假設:y)")
    t2 = backlog.add("人工處置過不碰")
    backlog.set_status(t2["id"], "parked", note="[手動] 歸檔")
    # set_status 會刷新 updated_at → 直接改檔把時間戳做舊
    import json

    old = time.time() - 25 * 3600
    data = backlog._load(None, mutable=True)
    for task in data["tasks"]:
        task["updated_at"] = old
    backlog._path(None).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    backlog._read_cache.clear()

    n = autopilot._maybe_clarify_timeout(now=time.time())
    assert n == 1
    tasks = {t["title"]: t for t in backlog.list_tasks()}
    assert tasks["逾時該復活"]["status"] == "pending"
    assert autopilot._CLARIFY_TIMEOUT_NOTE in tasks["逾時該復活"]["note"]
    assert tasks["人工處置過不碰"]["status"] == "parked"


def test_timeout_sweep_throttled_and_flag_off(monkeypatch):
    assert autopilot._maybe_clarify_timeout(now=1000.0) == 0  # 空 backlog
    assert autopilot._maybe_clarify_timeout(now=1100.0) == 0, "15 分鐘內不重掃"
    monkeypatch.setattr(config, "CLARIFY_ASYNC", False)
    monkeypatch.setattr(autopilot, "_clarify_sweep_at", 0.0)
    assert autopilot._maybe_clarify_timeout(now=99999.0) == 0


def test_requirement_section_with_answer():
    task = {"clarify": "問:範圍?(假設:全部)", "note": "[手動] 取回:只動 studio/"}
    s = autopilot._clarify_requirement_section(task)
    assert "範圍?" in s and "人工回覆:只動 studio/" in s
    assert autopilot._clarify_requirement_section({"note": "x"}) == ""


def test_timeout_sweep_swallows_write_errors(monkeypatch):
    """set_status 寫檔拋錯不得殺死主迴圈(對齊 sibling 慣例);下輪重掃可復原。"""
    t1 = backlog.add("會炸的任務")
    backlog.set_status(t1["id"], "parked", note="[待澄清] 問:x(假設:y)")
    import json

    old = time.time() - 25 * 3600
    data = backlog._load(None, mutable=True)
    for task in data["tasks"]:
        task["updated_at"] = old
    backlog._path(None).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    backlog._read_cache.clear()

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(autopilot.backlog, "set_status", boom)
    assert autopilot._maybe_clarify_timeout(now=time.time()) == 0  # 不得拋
