"""跨場次教訓庫的單元測試（純檔案 IO，不需 LLM）。"""

from __future__ import annotations

import json

import pytest

from studio import config, lessons
from studio.orchestrator import parse_lessons


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "LESSONS_FILE", tmp_path / "lessons.json")
    monkeypatch.setattr(config, "LESSONS_ENABLED", True)
    monkeypatch.setattr(config, "LESSONS_MAX", 12)
    return tmp_path


# === 解析 =============================================================


def test_parse_lessons_extracts_lines():
    text = (
        "檢討：整體不錯。\n"
        "教訓: 用 pathlib 比字串拼路徑更穩\n"
        "後續任務: 補測試\n"
        "教訓：httpx 逾時要設 timeout，否則會卡死\n"
    )
    assert parse_lessons(text) == [
        "用 pathlib 比字串拼路徑更穩",
        "httpx 逾時要設 timeout，否則會卡死",
    ]


def test_parse_lessons_none():
    assert parse_lessons("這次沒有特別的教訓") == []


def test_parse_lessons_capped_at_five():
    text = "\n".join(f"教訓: 第 {i} 條" for i in range(8))
    assert len(parse_lessons(text)) == 5


# === 持久化 + 去重 ====================================================


def test_add_and_recent(store):
    assert lessons.add_many(["A 經驗", "B 經驗"], session_id="s1", requirement="做個 X") == 2
    rows = lessons.recent(10)
    assert [r["text"] for r in rows] == ["B 經驗", "A 經驗"]  # 由新到舊
    assert rows[0]["session_id"] == "s1"
    assert rows[0]["requirement"] == "做個 X"
    assert rows[0]["source"] == "retro"


def test_add_many_persists_source(store):
    assert lessons.add_many(["表決先例: 技術選型 → SQLite"], source="vote") == 1
    assert lessons.all_lessons()[0]["source"] == "vote"


def test_dedup_exact_text(store):
    lessons.add_many(["重複教訓"])
    assert lessons.add_many(["重複教訓", "新教訓"]) == 1  # 只加新的
    assert len(lessons.all_lessons()) == 2


def test_dedup_fuzzy_near_duplicate(store):
    lessons.add_many(["浮點比較要用 math.isclose，別用 == 避免精度誤差"])
    assert lessons.add_many(["浮點數比較要用 math.isclose，不要用 ==，避免精度誤差"]) == 0
    assert len(lessons.all_lessons()) == 1


def test_dedup_fuzzy_keeps_different_lesson(store):
    lessons.add_many(["用 pathlib 比字串拼路徑更穩"])
    assert lessons.add_many(["httpx 逾時要設 timeout，否則會卡死"]) == 1
    assert len(lessons.all_lessons()) == 2


def test_exact_only_keeps_vote_template_with_different_winner(store):
    lessons.add_many(["表決先例: UI 技術 → A"], source="vote", exact_only=True)
    assert lessons.add_many(["表決先例: UI 技術 → B"], source="vote", exact_only=True) == 1
    assert [r["text"] for r in lessons.all_lessons()] == [
        "表決先例: UI 技術 → A",
        "表決先例: UI 技術 → B",
    ]


def test_empty_and_blank_skipped(store):
    assert lessons.add_many(["", "   ", "\n"]) == 0
    assert lessons.all_lessons() == []


def test_recent_limit(store):
    lessons.add_many([f"教訓 {i}" for i in range(5)])
    assert [r["text"] for r in lessons.recent(2)] == ["教訓 4", "教訓 3"]
    assert lessons.recent(0) == []


def test_max_store_trims(store, monkeypatch):
    monkeypatch.setattr(lessons, "_MAX_STORE", 3)
    lessons.add_many([f"教訓 {i}" for i in range(6)])
    texts = [r["text"] for r in lessons.all_lessons()]
    assert texts == ["教訓 3", "教訓 4", "教訓 5"]  # 只留最新 3 筆（舊→新）


# === 注入文字 =========================================================


def test_context_block(store):
    lessons.add_many(["先教訓", "後教訓"])
    ctx = lessons.context()
    assert "跨場次教訓庫" in ctx
    assert "後教訓" in ctx and "先教訓" in ctx
    assert ctx.index("後教訓") < ctx.index("先教訓")  # 最新在前
    assert ctx.endswith("\n\n")


def test_context_empty_when_no_lessons(store):
    assert lessons.context() == ""


def test_context_blank_when_disabled(store, monkeypatch):
    lessons.add_many(["有料的教訓"])
    monkeypatch.setattr(config, "LESSONS_ENABLED", False)
    assert lessons.context() == ""


def test_context_respects_max(store, monkeypatch):
    lessons.add_many([f"教訓 {i}" for i in range(5)])
    monkeypatch.setattr(config, "LESSONS_MAX", 2)
    ctx = lessons.context()
    assert "教訓 4" in ctx and "教訓 3" in ctx
    assert "教訓 0" not in ctx and "教訓 2" not in ctx


def test_old_format_without_source_is_readable(store):
    config.LESSONS_FILE.write_text(
        json.dumps({"lessons": [{"text": "舊教訓", "created_at": 1.0}]}, ensure_ascii=False),
        encoding="utf-8",
    )
    assert "舊教訓" in lessons.context()
