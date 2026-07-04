"""lessons.distill() 語意蒸餾 + scope/use_count 測試（純檔案 IO，LLM 以注入縫替身）。

核心不變式：壞輸出/離線/異常一律保留原庫——絕不讓蒸餾清空長期記憶。
"""

from __future__ import annotations

import json

import pytest

from studio import config, lessons, providers


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "LESSONS_FILE", tmp_path / "lessons.json")
    monkeypatch.setattr(config, "LESSONS_ENABLED", True)
    monkeypatch.setattr(config, "LESSONS_MAX", 12)
    monkeypatch.setattr(config, "LESSONS_DISTILL", True)
    monkeypatch.setattr(config, "LESSONS_DISTILL_THRESHOLD", 5)
    monkeypatch.setattr(config, "LESSONS_DISTILL_INTERVAL", 0)
    return tmp_path


def _seed(n: int) -> None:
    lessons.add_many([f"教訓內容第{i}條（彼此不同）" for i in range(n)])


def _fake_returning(text: str):
    async def _fake(**kw):
        return text

    return _fake


def _texts() -> list[str]:
    return [it["text"] for it in lessons.all_lessons()]


# ---------- 前置閘 ----------
async def test_below_threshold_noop(store, monkeypatch):
    _seed(3)  # < THRESHOLD(5)
    called = {"n": 0}

    async def fake(**kw):
        called["n"] += 1
        return "教訓: x"

    monkeypatch.setattr(providers, "complete_once", fake)
    assert await lessons.distill() == 0
    assert called["n"] == 0  # LLM 根本不該被呼叫
    assert len(lessons.all_lessons()) == 3


# ---------- 正常蒸餾 + 間隔阻擋 ----------
async def test_normal_distill_then_interval_blocks(store, monkeypatch):
    monkeypatch.setattr(config, "LESSONS_DISTILL_INTERVAL", 86400)
    _seed(8)
    monkeypatch.setattr(
        providers, "complete_once", _fake_returning("教訓: 合併甲\n教訓: 合併乙\n教訓: 合併丙\n")
    )
    removed = await lessons.distill(session_id="s1")
    assert removed == 5  # 8 → 3
    assert _texts() == ["合併甲", "合併乙", "合併丙"]
    assert {it["source"] for it in lessons.all_lessons()} == {"retro"}
    # meta.last_distill_at 已寫；緊接再呼叫（INTERVAL=86400）→ 不重跑
    assert await lessons.distill(session_id="s1") == 0
    assert len(lessons.all_lessons()) == 3


# ---------- 資料安全閘：保留原庫 ----------
async def test_empty_llm_keeps_store(store, monkeypatch):
    _seed(8)
    monkeypatch.setattr(providers, "complete_once", _fake_returning(""))  # 離線語意
    assert await lessons.distill() == 0
    assert len(lessons.all_lessons()) == 8


async def test_garbage_output_keeps_store(store, monkeypatch):
    _seed(8)
    monkeypatch.setattr(providers, "complete_once", _fake_returning("這不是格式\n隨便講講\n"))
    assert await lessons.distill() == 0
    assert len(lessons.all_lessons()) == 8


async def test_mass_deletion_rejected(store, monkeypatch):
    """疑似大規模誤刪（< 快照 ×20%）→ 保留原庫。"""
    _seed(10)  # floor = max(1, int(10*0.2)) = 2
    monkeypatch.setattr(providers, "complete_once", _fake_returning("教訓: 只剩一條\n"))
    assert await lessons.distill() == 0
    assert len(lessons.all_lessons()) == 10


async def test_no_reduction_rejected(store, monkeypatch):
    """筆數未減少（>= 快照）→ 非蒸餾，保留原庫。"""
    _seed(6)
    out = "\n".join(f"教訓: 改寫{i}" for i in range(6))  # 6 條，不減少
    monkeypatch.setattr(providers, "complete_once", _fake_returning(out))
    assert await lessons.distill() == 0
    assert len(lessons.all_lessons()) == 6


# ---------- 併發保留 ----------
async def test_concurrent_addition_preserved(store, monkeypatch):
    _seed(8)

    async def fake(**kw):
        # 蒸餾期間（鎖外）其他 session 新增的教訓，套用時不可被蓋掉。
        lessons.add_many(["蒸餾期間新教訓"])
        return "教訓: 甲\n教訓: 乙\n教訓: 丙\n"

    monkeypatch.setattr(providers, "complete_once", fake)
    await lessons.distill()
    texts = _texts()
    assert "蒸餾期間新教訓" in texts
    assert {"甲", "乙", "丙"} <= set(texts)


# ---------- project-scope 不參與蒸餾、原樣保留 ----------
async def test_project_scope_preserved(store, monkeypatch):
    _seed(8)  # global
    lessons.add_many(["專案專屬A", "專案專屬B"], scope="proj-x")
    monkeypatch.setattr(providers, "complete_once", _fake_returning("教訓: 甲\n教訓: 乙\n"))
    await lessons.distill()
    texts = _texts()
    assert "專案專屬A" in texts and "專案專屬B" in texts  # project 筆原樣保留
    assert {"甲", "乙"} <= set(texts)  # global 已蒸餾
    assert "教訓內容第0條（彼此不同）" not in texts  # 原 global 快照被取代


# ---------- scope 過濾與 use_count ----------
def test_scope_filter_and_use_count(store):
    lessons.add_many(["全域教訓"])  # 預設 global
    lessons.add_many(["專案教訓"], scope="proj-x")

    # 預設 scope="" → 只取 global
    ctx = lessons.context()
    assert "全域教訓" in ctx and "專案教訓" not in ctx

    # scope="proj-x" → global + proj-x
    ctx2 = lessons.context(scope="proj-x")
    assert "全域教訓" in ctx2 and "專案教訓" in ctx2

    by_text = {it["text"]: it for it in lessons.all_lessons()}
    assert by_text["全域教訓"]["use_count"] == 2  # 兩次 context 都選到
    assert by_text["專案教訓"]["use_count"] == 1  # 僅第二次選到
    assert by_text["專案教訓"]["scope"] == "proj-x"
    assert by_text["全域教訓"]["scope"] == "global"


def test_old_format_treated_as_global(store):
    """舊資料無 scope 鍵 → 視為 global、可被預設 scope 取到（零遷移）。"""
    config.LESSONS_FILE.write_text(
        json.dumps({"lessons": [{"text": "舊教訓", "created_at": 1.0}]}, ensure_ascii=False),
        encoding="utf-8",
    )
    rows = lessons.recent(5)  # scope="" 預設
    assert any(r["text"] == "舊教訓" for r in rows)
    # context 選中後 use_count 由「無鍵」起算 +1
    lessons.context()
    by_text = {it["text"]: it for it in lessons.all_lessons()}
    assert by_text["舊教訓"]["use_count"] == 1


async def test_distill_disabled_short_circuits(store, monkeypatch):
    monkeypatch.setattr(config, "LESSONS_DISTILL", False)
    _seed(8)
    called = {"n": 0}

    async def fake(**kw):
        called["n"] += 1
        return "教訓: x"

    monkeypatch.setattr(providers, "complete_once", fake)
    assert await lessons.distill() == 0
    assert called["n"] == 0
    assert len(lessons.all_lessons()) == 8
