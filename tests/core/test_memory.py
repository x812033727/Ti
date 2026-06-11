"""任務級反思記憶（studio/memory.py）單元測試 —— 移植自 ti-studio 交付的記憶測試並擴充。

涵蓋：寫/讀、跨任務隔離、before_round、exclude_latest 語意、去重保最新、recent_n（0 回空）、
token 預算、0600 權限、壞行跳過、持久化、delete、並行寫入無遺漏，以及 history GC 連帶刪除。
"""

from __future__ import annotations

import asyncio
import os

import pytest

from studio import config, memory


@pytest.fixture
def hist(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HISTORY_ROOT", tmp_path)
    monkeypatch.setattr(config, "REFLEXION_ENABLED", True)
    return tmp_path


def test_write_then_retrieve(hist):
    sid = "s1"
    assert memory.write(sid, 1, "反思A", round_no=1) is True
    assert memory.write(sid, 1, "   ", round_no=2) is False  # 空白略過
    assert memory.write(sid, 1, "", round_no=3) is False
    out = memory.retrieve(sid, 1)
    assert len(out) == 1
    assert out[0]["content"] == "反思A" and out[0]["round"] == 1 and out[0]["task_id"] == 1


def test_task_isolation_and_before_round(hist):
    sid = "s2"
    memory.write(sid, 1, "t1r1", round_no=1)
    memory.write(sid, 2, "t2r1", round_no=1)
    memory.write(sid, 1, "t1r2", round_no=2)
    assert [r["content"] for r in memory.retrieve(sid, 1)] == ["t1r1", "t1r2"]
    assert [r["content"] for r in memory.retrieve(sid, 2)] == ["t2r1"]
    assert len(memory.retrieve(sid, None)) == 3  # 不過濾任務取全部
    assert [r["content"] for r in memory.retrieve(sid, 1, before_round=2)] == ["t1r1"]


def test_build_context_exclude_latest(hist):
    sid = "s3"
    memory.write(sid, 1, "第1輪心得", round_no=1)
    memory.write(sid, 1, "第2輪心得", round_no=2)
    ctx = memory.build_context(sid, 1, exclude_latest=True)
    assert "第1輪心得" in ctx and "第2輪心得" not in ctx  # 最新一筆已由 verbatim feedback 帶入
    assert memory.DEFAULT_HEADER in ctx and ctx.endswith("\n\n")  # 可直接前接後文
    ctx_all = memory.build_context(sid, 1, exclude_latest=False)
    assert "第1輪心得" in ctx_all and "第2輪心得" in ctx_all  # huddle 路徑全帶


def test_build_context_empty_when_disabled(hist, monkeypatch):
    monkeypatch.setattr(config, "REFLEXION_ENABLED", False)
    sid = "s4"
    memory.write(sid, 1, "x", round_no=1)
    assert memory.build_context(sid, 1) == ""


def test_build_context_empty_when_no_record(hist):
    assert memory.build_context("none", 99) == ""


def test_build_context_dedup_keeps_newest(hist):
    sid = "s5"
    for i in (1, 2, 3):
        memory.write(sid, 1, "重複反思", round_no=i)
    memory.write(sid, 1, "新反思", round_no=4)  # 最新一筆（會被 exclude_latest 排除）
    ctx = memory.build_context(sid, 1, exclude_latest=True)
    assert ctx.count("重複反思") == 1  # 去重


def test_build_context_recent_n_zero_returns_empty(hist):
    sid = "s6"
    for i in (1, 2, 3):
        memory.write(sid, 1, f"r{i}", round_no=i)
    # 回歸：recent_n=0 必須回空，不可退化成全帶（與「關閉注入」職責一致）。
    assert memory.build_context(sid, 1, recent_n=0) == ""


def test_build_context_recent_n_keeps_latest(hist):
    sid = "s7"
    for i in range(6):
        memory.write(sid, 1, f"反思{i}", round_no=i)
    # exclude_latest 去掉 反思5；recent_n=2 取剩餘最新兩筆 → 反思3、反思4（依時間序輸出）。
    ctx = memory.build_context(sid, 1, exclude_latest=True, recent_n=2, token_budget=10000)
    assert "反思4" in ctx and "反思3" in ctx and "反思2" not in ctx
    assert ctx.index("反思3") < ctx.index("反思4")


def test_build_context_token_budget(hist):
    sid = "s8"
    for i in range(20):
        memory.write(sid, 1, "X" * 200 + f"#{i}", round_no=i)
    big = memory.build_context(sid, 1, exclude_latest=False, recent_n=20, token_budget=10000)
    small = memory.build_context(sid, 1, exclude_latest=False, recent_n=20, token_budget=50)
    assert len(small) < len(big)
    assert "#19" in small  # 小預算至少仍納入最新一條（避免回空）


def test_file_permission_0600(hist):
    sid = "s9"
    memory.write(sid, 1, "含敏感反思", round_no=1)
    mode = os.stat(memory.memory_path(sid)).st_mode & 0o777
    assert mode == 0o600


def test_corrupt_line_skipped(hist):
    sid = "s10"
    memory.write(sid, 1, "正常一筆", round_no=1)
    with memory.memory_path(sid).open("a", encoding="utf-8") as f:
        f.write("這不是 json{{{\n")
        f.write("\n")
    out = memory.retrieve(sid, 1)
    assert len(out) == 1 and out[0]["content"] == "正常一筆"


def test_persist_across_reread(hist):
    sid = "s11"
    memory.write(sid, 1, "重啟後要讀得到", round_no=1)
    assert memory.retrieve(sid, 1)[0]["content"] == "重啟後要讀得到"  # 同 path 重讀即持久化


def test_delete_removes_files(hist):
    sid = "s12"
    memory.write(sid, 1, "x", round_no=1)
    memory.build_context(sid, 1)  # 觸發 lock 檔建立
    assert memory.memory_path(sid).exists()
    memory.delete(sid)
    assert not memory.memory_path(sid).exists()
    assert not memory._lock_path(sid).exists()


async def test_concurrent_writers_no_loss(hist):
    sid = "s13"

    async def writer(n: int) -> None:
        for i in range(10):
            memory.write(sid, 1, f"w{n}-{i}", round_no=i)

    await asyncio.gather(*[writer(n) for n in range(4)])
    assert len(memory.retrieve(sid, 1)) == 40  # fcntl 鎖序列化 append，無遺漏


def test_history_delete_session_removes_memory(hist, monkeypatch):
    from studio import history

    monkeypatch.setattr(config, "WORKSPACE_ROOT", hist / "ws")
    sid = "hsid"
    history.start_session(sid, "需求")
    history.finish_session(sid)  # 無事件 → status=incomplete（非 running，可刪）
    memory.write(sid, 1, "反思", round_no=1)
    assert memory.memory_path(sid).exists()
    assert history.delete_session(sid) is True
    assert not memory.memory_path(sid).exists()  # GC 連帶回收，不洩漏
