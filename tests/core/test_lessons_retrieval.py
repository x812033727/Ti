"""教訓庫相關性檢索測試：中英輕量斷詞、相關優先於新舊、無相關退回最新 N 筆。"""

from __future__ import annotations

import pytest

from studio import config, lessons


@pytest.fixture(autouse=True)
def _tmp_lessons(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "LESSONS_FILE", tmp_path / "lessons.json")
    monkeypatch.setattr(config, "LESSONS_ENABLED", True)
    monkeypatch.setattr(config, "LESSONS_MAX", 3)
    yield


def test_tokens_mixed_zh_en():
    toks = lessons._tokens("無人機用 MAVLink 協定")
    assert "無人" in toks and "人機" in toks  # 中文 bigram
    assert "mavlink" in toks  # ASCII 詞（小寫化）


def test_relevant_picks_topic_over_recency():
    # 先存「無人機」教訓（較舊），再灌一堆網站教訓（較新）
    lessons.add_many(["無人機航線規劃要先驗證 GPS 精度"], requirement="做無人機地面站")
    lessons.add_many(
        [f"網站表單驗證要做第 {i} 種邊界" for i in range(10)],
        requirement="做一個會員網站",
    )
    rows = lessons.relevant(3, "做一個無人機巡檢產品")
    assert rows, "應找得到相關教訓"
    assert "無人機" in rows[0]["text"]  # 主題相關擊敗「比較新」


def test_context_relevance_mode_and_fallback():
    lessons.add_many(["無人機要注意 GPS"], requirement="無人機")
    lessons.add_many([f"網站教訓 {i}" for i in range(5)], requirement="網站")

    # 有相關 → 按相關性挑選，標示挑選模式
    ctx = lessons.context(requirement="做無人機")
    assert "依本次需求相關性挑選" in ctx
    assert "GPS" in ctx

    # 完全無相關 → 退回最新 N 筆（原行為），不標相關性
    ctx2 = lessons.context(requirement="ABCXYZ")
    assert "最新數筆" in ctx2
    assert "網站教訓 4" in ctx2  # 最新的網站教訓

    # 未給需求 → 同樣走最新 N 筆（向後相容）
    ctx3 = lessons.context()
    assert "最新數筆" in ctx3


def test_context_disabled_or_empty(monkeypatch):
    assert lessons.context(requirement="任何需求") == ""  # 庫是空的
    lessons.add_many(["某教訓"], requirement="x")
    monkeypatch.setattr(config, "LESSONS_ENABLED", False)
    assert lessons.context(requirement="某") == ""  # 停用


def test_relevant_zero_limit():
    lessons.add_many(["a 教訓"], requirement="a")
    assert lessons.relevant(0, "a") == []
