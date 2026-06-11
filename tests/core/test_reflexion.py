"""Reflexion 反思（studio/reflexion.py）單元測試。

驗收：產出非空（LLM 回空/拋錯都 fallback）、永不 raise、長度上限、寫入記憶且可讀回、
prompt 帶入評審意見且系統指示「不裁決成敗/不給分數」（守住不自評獎勵）。
"""

from __future__ import annotations

import pytest

from studio import config, memory, reflexion


@pytest.fixture
def hist(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HISTORY_ROOT", tmp_path)
    monkeypatch.setattr(config, "REFLEXION_ENABLED", True)
    return tmp_path


def test_build_prompt_carries_feedback_forbids_verdict():
    system, user = reflexion.build_prompt("#1：sum_list", 1, "def f(): ...", "FAIL：空輸入炸")
    assert "FAIL：空輸入炸" in user  # 評審意見有進 prompt
    assert "不要宣稱通過與否" in system and "分數" in system  # 指示不裁決/不給分


async def test_reflect_and_store_good_llm(hist):
    async def good(system: str, user: str) -> str:
        return "  根因：未處理空陣列；下一輪先判長度為 0 再走主邏輯  "

    task = {"id": 7, "title": "sum_list"}
    text = await reflexion.reflect_and_store("s", task, 1, "code", "FAIL", llm=good)
    assert text.strip() and "根因" in text
    rows = memory.retrieve("s", 7)
    assert len(rows) == 1
    assert rows[0]["content"].startswith("[第 1 輪反思]") and text in rows[0]["content"]


async def test_fallback_on_empty_llm(hist):
    async def empty(system: str, user: str) -> str:
        return "   "

    text = await reflexion.reflect_and_store(
        "s", {"id": 1, "title": "x"}, 1, "c", "評審指出邊界沒處理", llm=empty
    )
    assert text.strip()  # 仍保證非空（fallback）
    assert "評審指出邊界沒處理" in text or "修正" in text


async def test_never_raises_on_llm_exception(hist):
    async def boom(system: str, user: str) -> str:
        raise RuntimeError("llm down")

    text = await reflexion.reflect_and_store("s", {"id": 1, "title": "x"}, 1, "c", "fb", llm=boom)
    assert text.strip()  # LLM 拋錯也有保底、且不 raise
    assert len(memory.retrieve("s", 1)) == 1  # 仍寫入


async def test_long_reflection_capped(hist):
    async def long_llm(system: str, user: str) -> str:
        return "字" * (reflexion.MAX_REFLECTION_CHARS + 500)

    text = await reflexion.reflect_and_store(
        "s", {"id": 1, "title": "x"}, 1, "c", "fb", llm=long_llm
    )
    assert len(text) <= reflexion.MAX_REFLECTION_CHARS + 1  # +1 容納省略號


async def test_disabled_reflexion_still_stores_when_called(hist, monkeypatch):
    # reflect_and_store 本身不檢查開關（由 orchestrator._store_reflection 守門）；直接呼叫仍寫入。
    async def good(system: str, user: str) -> str:
        return "反思內容"

    monkeypatch.setattr(config, "REFLEXION_ENABLED", False)
    await reflexion.reflect_and_store("s", {"id": 2, "title": "y"}, 1, "c", "fb", llm=good)
    assert len(memory.retrieve("s", 2)) == 1
