"""調查任務分流輕量管線（完成率第三輪修法一）。

背景（14 筆「討論未達完成」failed 全數 session log 驗屍）：9 筆是純調查/驗證/證據儀式型
——正確完成判準是「產出結構化結論」而非「code 過三審＋Demo」，卻被送進多專家全套管線
（$TMPDIR 落檔 QA 讀不到 → 每輪 FAIL 同因，結構上不可能過），每筆重燒 2×~100 分鐘 session。

本檔驗證：
1. `_is_investigation_task` 確定性分類（真實失敗任務標題當黑白樣本；旋鈕/升級標記）。
2. `_run_investigation_task` 四出口：結論→done＋lessons＋followups；需人工→parked；
   需改碼→退回 pending＋lane=full＋不耗 attempts；空/缺結論/缺證據→討論未收斂重試語意。
3. run_one_task 分流命中時**完全不建 StudioSession**（零多專家成本、不開 PR）。

範式沿用 test_timeout_autosplit.py：tmp state dir + mock Expert.speak，不打 LLM/網路。
"""

from __future__ import annotations

import json

import pytest

from studio import autopilot, backlog, config

# 驗屍取得的真實失敗任務標題（黑樣本＝該分流）
_REAL_INVESTIGATION_TITLES = [
    "彙整成單一權威報告檔（失敗測試名／訊息／行號），自帶唯一權威聲明＋sha256",
    "離線實跑測試並將結果落成證據檔供驗收",
    "調查 autopilot 逾時任務的根因並歸因",
    "盤點 backlog 中重複任務並回報清單",
]
# 白樣本＝有實作動詞，不該分流
_REAL_CODE_TITLES = [
    "實作 git 憑證注入層並補單測",
    "修復 B 的競態並加守門測試",
    "重構 orchestrator 派工邏輯",
]


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_STATE_DIR", tmp_path / "ap")
    monkeypatch.setattr(config, "HISTORY_ROOT", tmp_path / "hist")
    monkeypatch.setattr(config, "AUTOPILOT_DRYRUN", False)
    monkeypatch.setattr(config, "AUTOPILOT_INVESTIGATION_LANE", True)
    monkeypatch.setattr(config, "AUTOPILOT_INVESTIGATION_TIMEOUT", 30)
    monkeypatch.setattr(config, "AUTOPILOT_DISCUSSION_MAX_ATTEMPTS", 2)
    return tmp_path


def _patch_expert(monkeypatch, reply: str):
    import studio.experts as experts_mod

    class _FakeExpert:
        last_prompt = ""

        def __init__(self, *a, **k):
            pass

        async def speak(self, prompt, on_event):
            type(self).last_prompt = prompt
            return reply

        async def stop(self):
            return None

    monkeypatch.setattr(experts_mod, "Expert", _FakeExpert)
    return _FakeExpert


def _mk_task(title="調查 X 的根因並回報", detail=""):
    t = backlog.add(title, detail=detail)
    return t


def _load(task_id):
    return next(t for t in backlog.list_tasks() if t["id"] == task_id)


def _audit_lines(tmp_path):
    p = tmp_path / "ap" / "audit.jsonl"
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]


# --- 分類器 ---------------------------------------------------------------


def test_classifier_flags_real_investigation_titles(state):
    for title in _REAL_INVESTIGATION_TITLES:
        assert autopilot._is_investigation_task({"title": title, "detail": ""}), title


def test_classifier_exempts_codework_titles(state):
    for title in _REAL_CODE_TITLES:
        assert not autopilot._is_investigation_task({"title": title, "detail": ""}), title


def test_classifier_respects_kill_switch_and_escalation_mark(state, monkeypatch):
    task = {"title": _REAL_INVESTIGATION_TITLES[0], "detail": ""}
    assert autopilot._is_investigation_task(task)
    # 升級標記（前次調查判定需改碼）→ 不再分流，防乒乓
    assert not autopilot._is_investigation_task({**task, "lane": "full"})
    # 旋鈕關閉 → 恆 False（恢復現行為）
    monkeypatch.setattr(config, "AUTOPILOT_INVESTIGATION_LANE", False)
    assert not autopilot._is_investigation_task(task)


def test_prompt_forbids_file_drop_and_requires_markers(state):
    p = autopilot._build_investigation_prompt({"title": "調查 A", "detail": "細節 B"})
    assert "調查 A" in p and "細節 B" in p
    assert "$TMPDIR" in p, "須明令禁止 $TMPDIR 落檔（驗屍死因）"
    assert "結論:" in p and "證據:" in p and "需人工:" in p and "需改碼:" in p


# --- 四出口 ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_conclusion_marks_done_with_lessons_and_followups(state, monkeypatch, tmp_path):
    import studio.lessons as lessons_mod

    recorded: list = []
    monkeypatch.setattr(
        lessons_mod, "add_many", lambda texts, **kw: recorded.append((texts, kw)) or 1
    )
    _patch_expert(
        monkeypatch,
        "結論: 根因是 watchdog 未涵蓋 fetch 階段\n"
        "證據: studio/runner.py:120\n"
        "後續任務: 實作 fetch 階段 watchdog 並補守門測試\n",
    )
    task = _mk_task()
    await autopilot._run_investigation_task(task, "/clone", "sid-inv-1", 0.0)

    updated = _load(task["id"])
    assert updated["status"] == "done"
    assert "[調查結論]" in updated["note"] and "watchdog" in updated["note"]
    assert recorded and "根因是 watchdog" in recorded[0][0][0], "結論須沉澱進教訓庫"
    titles = [t["title"] for t in backlog.list_tasks()]
    assert "實作 fetch 階段 watchdog 並補守門測試" in titles, "後續任務須回填 backlog"
    audits = _audit_lines(tmp_path)
    assert audits and audits[-1]["outcome"] == "investigation_done"
    assert audits[-1]["pr"] is None, "調查管線不開 PR，不得計每日 PR 預算"


@pytest.mark.asyncio
async def test_needs_human_parks_task(state, monkeypatch, tmp_path):
    _patch_expert(monkeypatch, "需人工: 需要到 GitHub 後台換發 token")
    task = _mk_task("確認 GitHub token 是否需人工輪替")
    await autopilot._run_investigation_task(task, "/clone", "sid-inv-2", 0.0)

    updated = _load(task["id"])
    assert updated["status"] == "parked"
    assert "需人工" in updated["note"]
    assert _audit_lines(tmp_path)[-1]["outcome"] == "investigation_parked"


@pytest.mark.asyncio
async def test_needs_code_escalates_without_burning_attempts(state, monkeypatch, tmp_path):
    _patch_expert(monkeypatch, "需改碼: 要改 runner 重試邏輯才算完成")
    task = _mk_task("驗證 runner 的重試邏輯是否正確")
    # 模擬揀起：in_progress 會 attempts+1；傳入的 task dict 保持揀起前快照（attempts=0）
    backlog.set_status(task["id"], "in_progress")
    await autopilot._run_investigation_task(task, "/clone", "sid-inv-3", 0.0)

    updated = _load(task["id"])
    assert updated["status"] == "pending"
    assert updated["lane"] == "full", "升級標記防止再被分流"
    assert int(updated["attempts"]) == 0, "誤分類安全閥不得消耗 attempts"
    assert not autopilot._is_investigation_task(updated), "升級後不再分流（防乒乓）"
    assert _audit_lines(tmp_path)[-1]["outcome"] == "investigation_escalated"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reply", "why_hint"),
    [
        ("", "缺「結論:」"),
        ("我查完了但沒有照格式輸出", "缺「結論:」"),
        ("結論: 有結論但沒有任何佐證", "缺「證據:」"),
    ],
)
async def test_missing_conclusion_or_evidence_falls_back_to_retry(
    state, monkeypatch, reply, why_hint
):
    _patch_expert(monkeypatch, reply)
    task = _mk_task()
    backlog.set_status(task["id"], "in_progress")
    await autopilot._run_investigation_task(task, "/clone", "sid-inv-4", 0.0)

    updated = _load(task["id"])
    assert updated["status"] == "pending", "沿用討論未收斂的有限重試語意"
    assert "討論未達完成" in updated["note"], "note 子串不變，分診/看板無縫續接"
    assert why_hint in updated["note"], "裁決原因須寫進 note"


@pytest.mark.asyncio
async def test_expert_exception_falls_back_to_retry_not_crash(state, monkeypatch):
    import studio.experts as experts_mod

    class _BoomExpert:
        def __init__(self, *a, **k):
            pass

        async def speak(self, prompt, on_event):
            raise RuntimeError("provider down")

        async def stop(self):
            return None

    monkeypatch.setattr(experts_mod, "Expert", _BoomExpert)
    task = _mk_task()
    await autopilot._run_investigation_task(task, "/clone", "sid-inv-5", 0.0)
    assert _load(task["id"])["status"] == "pending", "專家例外不得冒泡弄死主迴圈"


# --- run_one_task 接線：分流命中時零 StudioSession ---------------------------


@pytest.mark.asyncio
async def test_run_one_task_routes_to_lane_without_studio_session(state, monkeypatch):
    async def _fake_clone(*_a, **_k):
        return "/tmp/does-not-matter"

    class _BoomSession:
        def __init__(self, *a, **k):
            raise AssertionError("調查分流命中時不得建 StudioSession（多專家 session）")

    monkeypatch.setattr(autopilot, "_prepare_clone", _fake_clone)
    monkeypatch.setattr(autopilot, "StudioSession", _BoomSession)
    _patch_expert(monkeypatch, "結論: 已查明根因\n證據: studio/x.py:1\n")

    task = _mk_task("調查 autopilot 逾時任務的根因並歸因")
    picked = backlog.next_pending()
    assert picked["id"] == task["id"]
    await autopilot.run_one_task(picked)

    assert _load(task["id"])["status"] == "done"
