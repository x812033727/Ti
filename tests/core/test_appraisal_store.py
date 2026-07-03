"""考核庫（studio/appraisal.py）的單元測試——純檔案 IO，不需 LLM。

鏡射 tests/core/test_lessons.py：record/summary/recent、檔案上限裁剪、壞檔容錯與
檔案鎖（多執行緒併發 read-modify-write 不丟筆）。
"""

from __future__ import annotations

import json
import threading
import time

import pytest

from studio import appraisal, config


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APPRAISALS_FILE", tmp_path / "appraisals.json")
    monkeypatch.setattr(config, "APPRAISAL_MAX_STORE", 2000)
    return tmp_path


def _entry(**over) -> dict:
    base = {
        "session_id": "s1",
        "task_id": 1,
        "role": "engineer",
        "provider": "claude",
        "model": "claude-opus-4-8",
        "score": 4,
        "comment": "穩定",
        "objective": {
            "qa_rounds": 2,
            "qa_passed": True,
            "senior_approved": True,
            "duration_s": 12.5,
        },
        "created_at": time.time(),
    }
    base.update(over)
    return base


# === record / recent ===================================================


def test_record_and_recent_order(store):
    appraisal.record([_entry(comment="第一筆", created_at=100.0)])
    appraisal.record([_entry(comment="第二筆", provider="codex", created_at=200.0)])
    rows = appraisal.recent(10)
    assert [r["comment"] for r in rows] == ["第二筆", "第一筆"]  # 由新到舊
    assert rows[0]["provider"] == "codex"
    assert rows[1]["session_id"] == "s1" and rows[1]["task_id"] == 1
    assert rows[1]["objective"]["qa_passed"] is True
    assert appraisal.recent(0) == []


def test_record_skips_invalid_entries(store):
    appraisal.record(
        [
            _entry(score=0),  # 分數越界
            _entry(score=6),
            _entry(score="abc"),  # 非數字
            _entry(provider="", role=""),  # provider 與 role 皆空
            "not-a-dict",  # 非 dict
            _entry(score=5, comment="唯一合法"),
        ]
    )
    rows = appraisal.all_appraisals()
    assert len(rows) == 1
    assert rows[0]["comment"] == "唯一合法" and rows[0]["score"] == 5


def test_record_fills_created_at_and_normalizes(store):
    t0 = time.time()
    appraisal.record(
        [_entry(created_at=None, provider=" Claude ", comment="  評語  ", objective="bad")]
    )
    row = appraisal.all_appraisals()[0]
    assert row["created_at"] >= t0
    assert row["provider"] == "claude"  # 正規化小寫、去空白
    assert row["comment"] == "評語"
    assert row["objective"] == {}  # 非 dict 的 objective 視為無客觀指標


def test_record_empty_is_noop(store):
    appraisal.record([])
    appraisal.record([_entry(score=9)])
    assert not config.APPRAISALS_FILE.exists()  # 全批無效不落檔


# === 檔案上限裁剪 =======================================================


def test_max_store_trims_oldest(store, monkeypatch):
    monkeypatch.setattr(config, "APPRAISAL_MAX_STORE", 3)
    appraisal.record([_entry(comment=f"第 {i} 筆", created_at=float(i)) for i in range(6)])
    rows = appraisal.all_appraisals()
    assert [r["comment"] for r in rows] == ["第 3 筆", "第 4 筆", "第 5 筆"]  # 只留最新 3 筆


# === summary 聚合 =======================================================


def test_summary_two_level_aggregation(store):
    appraisal.record(
        [
            _entry(score=5, objective={"qa_passed": True}),
            _entry(score=4, objective={"qa_passed": False}),
            _entry(provider="codex", model="", score=3, objective={"qa_passed": None}),
        ]
    )
    summ = appraisal.summary()
    assert summ["providers"]["claude"] == {"avg_score": 4.5, "n": 2, "pass_rate": 0.5}
    # codex 無客觀裁決樣本 → pass_rate None（不虛構通過率）。
    assert summ["providers"]["codex"] == {"avg_score": 3.0, "n": 1, "pass_rate": None}
    # 第二層：provider/model；model 空者不入此層。
    assert summ["models"]["claude/claude-opus-4-8"]["n"] == 2
    assert "codex/" not in "".join(summ["models"])


def test_summary_respects_limit_days(store):
    appraisal.record(
        [
            _entry(score=1, comment="舊", created_at=time.time() - 40 * 86400),
            _entry(score=5, comment="新"),
        ]
    )
    assert appraisal.summary()["providers"]["claude"]["n"] == 1  # 預設 30 天：舊筆不入
    assert appraisal.summary(limit_days=0)["providers"]["claude"]["n"] == 2  # 0＝不限


def test_summary_empty_store(store):
    assert appraisal.summary() == {"providers": {}, "models": {}}


# === 壞檔容錯 ===========================================================


def test_corrupt_file_tolerated_and_recoverable(store):
    config.APPRAISALS_FILE.write_text("{{{ 不是 JSON", encoding="utf-8")
    assert appraisal.summary() == {"providers": {}, "models": {}}
    assert appraisal.recent(5) == []
    appraisal.record([_entry()])  # 壞檔被覆寫、可繼續寫入
    assert len(appraisal.all_appraisals()) == 1


def test_wrong_shape_tolerated(store):
    config.APPRAISALS_FILE.write_text(json.dumps({"appraisals": "oops"}), encoding="utf-8")
    assert appraisal.all_appraisals() == []


# === 檔案鎖（併發 read-modify-write 不丟筆） =============================


def test_concurrent_record_loses_nothing(store):
    def worker(tag: str) -> None:
        for i in range(20):
            appraisal.record([_entry(comment=f"{tag}-{i}")])

    threads = [threading.Thread(target=worker, args=(f"t{n}",)) for n in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(appraisal.all_appraisals()) == 40  # flock 序列化：無 lost update
    assert config.APPRAISALS_FILE.with_suffix(".lock").exists()
