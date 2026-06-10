"""自我評估記憶（self-reinforcing）的單元測試：把迴圈自身近期成敗回饋進評估。

純檔案 IO、不需 LLM/網路；state dir 指向 tmp，直接驗證 backlog 衍生的記憶文字與去重過濾。
"""

from __future__ import annotations

import pytest

from studio import autopilot, backlog, config


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_STATE_DIR", tmp_path / "ap")
    monkeypatch.setattr(config, "AUTOPILOT_EVAL_MEMORY", 20)
    return tmp_path


def _seed(title: str, status: str, **fields):
    t = backlog.add(title)
    backlog.set_status(t["id"], status, **fields)
    return t


def test_empty_backlog_returns_blank(state):
    assert autopilot._recent_outcomes_context() == ""


def test_includes_done_and_failed_with_note(state):
    _seed("加上 X 測試", "done")
    _seed("重構 Y 模組", "failed", note="測試未通過")
    ctx = autopilot._recent_outcomes_context()
    assert "加上 X 測試" in ctx
    assert "重構 Y 模組" in ctx
    assert "測試未通過" in ctx
    # done 在「勿重複」段、failed 在「勿重蹈」段
    done_idx = ctx.index("已完成")
    failed_idx = ctx.index("失敗")
    assert ctx.index("加上 X 測試") > done_idx
    assert ctx.index("重構 Y 模組") > failed_idx


def test_recent_first_ordering(state):
    older = _seed("舊任務", "done")
    newer = _seed("新任務", "done")
    # 確保 updated_at 嚴格遞增（避免同秒）
    backlog.set_status(older["id"], "done", updated_at=100.0)
    backlog.set_status(newer["id"], "done", updated_at=200.0)
    ctx = autopilot._recent_outcomes_context()
    assert ctx.index("新任務") < ctx.index("舊任務")


def test_respects_limit(state, monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_EVAL_MEMORY", 2)
    for i in range(5):
        t = _seed(f"完成 {i}", "done")
        backlog.set_status(t["id"], "done", updated_at=float(i))
    ctx = autopilot._recent_outcomes_context()
    # 只保留最新 2 筆（完成 4、完成 3）
    assert "完成 4" in ctx and "完成 3" in ctx
    assert "完成 0" not in ctx and "完成 1" not in ctx and "完成 2" not in ctx


def test_zero_disables(state, monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_EVAL_MEMORY", 0)
    _seed("做過的事", "done")
    assert autopilot._recent_outcomes_context() == ""
    assert autopilot._recent_done_titles() == set()


def test_recent_done_titles_for_filter(state):
    _seed("已完成的改善", "done")
    _seed("失敗的嘗試", "failed", note="x")
    titles = autopilot._recent_done_titles()
    assert "已完成的改善" in titles
    # failed 不在 done 過濾集合（失敗的做法允許帶新做法重提，由提示詞引導，不在此硬擋）
    assert "失敗的嘗試" not in titles
