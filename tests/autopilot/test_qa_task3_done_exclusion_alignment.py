"""QA 破壞性驗證（任務 #3 / 子題：兩道防線比對範圍對齊 pending+in_progress）。

獨立補強「done 任務天然排除於比對來源」這一反向證明——現有測試已證 in_progress 會
納入清單（test_autopilot_prefilter.test_filter_covers_in_progress），但缺少對等的
**反向格**：done 任務不得進入層① 相似度、層② 子系統計數、以及主動分散層
（_oversubscribed_context）。架構決策矩陣的第三格（3 done + 1 pending → 有效計數 1
→ 放行）正落在此缺口。

設計：層②/主動分散層本身不過濾狀態（純函式只統計傳入 list），狀態過濾由唯一來源
`_pending_titles()`（pending + in_progress）負責。故守門點是「來源排除 done」+「來源
餵入 filter/oversub 後行為正確」。

此檔只補測試、不改任何程式邏輯，亦不觸碰 backlog._is_duplicate 字串等值契約。
"""

from __future__ import annotations

import pytest

from studio import autopilot, backlog, config


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_STATE_DIR", tmp_path / "ap")
    monkeypatch.setattr(config, "AUTOPILOT_EVAL_MEMORY", 20)
    return tmp_path


# ---------------------------------------------------------------------------
# 來源排除：_pending_titles() 只含 pending + in_progress，done/failed 不入
# ---------------------------------------------------------------------------


def test_pending_titles_excludes_done_and_failed(state):
    backlog.add("pending 的 backlog 任務")
    ip = backlog.add("in_progress 的 backlog 任務")
    d = backlog.add("done 的 backlog 任務")
    f = backlog.add("failed 的 backlog 任務")
    backlog.set_status(ip["id"], "in_progress")
    backlog.set_status(d["id"], "done")
    backlog.set_status(f["id"], "failed")

    titles = autopilot._pending_titles()
    assert "pending 的 backlog 任務" in titles
    assert "in_progress 的 backlog 任務" in titles  # in_progress 納入（與 prompt 禁止清單對齊）
    assert "done 的 backlog 任務" not in titles  # done 排除
    assert "failed 的 backlog 任務" not in titles  # failed 排除


# ---------------------------------------------------------------------------
# 架構矩陣第三格：3 done + 1 pending（有效計數 1）→ 層② 放行
# done 若被誤算入層② coverage，計數=4 ≥ K=2 會誤擋；故此測證 done 確未被計入。
# ---------------------------------------------------------------------------


def test_layer2_done_not_counted_admits_proposal(state, monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_SUBSYSTEM_MAX_PENDING", 2)
    # 三筆 done backlog（不應計入）+ 一筆 pending backlog（有效計數 1）
    for t in ("已完成 backlog 甲", "已完成 backlog 乙", "已完成 backlog 丙"):
        d = backlog.add(t)
        backlog.set_status(d["id"], "done")
    backlog.add("在排隊的 backlog 改善")  # pending，計數 1

    existing = autopilot._pending_titles()
    assert existing == ["在排隊的 backlog 改善"]  # 來源已排除 3 筆 done

    # 提案同屬 backlog；有效計數 1 < K=2 → 放行。若 done 被誤算（4 ≥ 2）則會被擋。
    kept = autopilot._filter_pending_duplicates(["為 backlog 增補快照機制"], existing)
    assert kept == ["為 backlog 增補快照機制"]


def test_layer2_pending_only_at_k_still_blocks(state, monkeypatch):
    # 對照組（判別力 / 排除假綠）：把上題的 3 done 改成 pending，計數=K=2 → 必須被擋，
    # 證明上題的「放行」確實源於 done 被排除，而非 filter 失靈一律放行。
    monkeypatch.setattr(config, "AUTOPILOT_SUBSYSTEM_MAX_PENDING", 2)
    for t in ("在排隊 backlog 甲", "在排隊 backlog 乙"):
        backlog.add(t)  # 兩筆 pending backlog，計數達 K
    existing = autopilot._pending_titles()
    assert len(existing) == 2
    kept = autopilot._filter_pending_duplicates(["為 backlog 增補快照機制"], existing)
    assert kept == []  # 達 K → 擋


# ---------------------------------------------------------------------------
# 主動分散層（_oversubscribed_context / _build_discovery_prompt）同樣排除 done
# ---------------------------------------------------------------------------


def test_oversubscribed_excludes_done(state, monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_SUBSYSTEM_MAX", 2)
    # 3 筆 done backlog——若 done 進入來源會觸發「已過多」段；正確行為是完全不觸發。
    for t in ("done backlog 甲", "done backlog 乙", "done backlog 丙"):
        d = backlog.add(t)
        backlog.set_status(d["id"], "done")
    # _oversubscribed_context() 預設讀 _pending_titles()，done 已被排除 → 無超標子系統
    assert autopilot._oversubscribed_context() == ""
    # 端到端組裝層亦不得出現「已過多」段
    prompt = autopilot._build_discovery_prompt()
    assert "下列子系統的排隊任務已過多" not in prompt


def test_oversubscribed_counts_pending_over_quota(state, monkeypatch):
    # 對照組：同樣 3 筆但為 pending → 必須觸發超標段，證明上題的「不觸發」源於 done 排除。
    monkeypatch.setattr(config, "AUTOPILOT_SUBSYSTEM_MAX", 2)
    for t in ("pending backlog 甲", "pending backlog 乙", "pending backlog 丙"):
        backlog.add(t)
    ctx = autopilot._oversubscribed_context()
    assert "下列子系統的排隊任務已過多" in ctx
    assert "backlog（已有 3 筆）" in ctx


# ---------------------------------------------------------------------------
# 設計不變式：軟門檻（prompt 引導）須嚴格小於硬門檻（進場拒收），否則非對稱
# 分層靜默失效（軟提醒比硬擋還晚觸發）。鎖死兩常數的相對關係（與 config import-time
# assert 互補：此處在測試層明示，CI 紅燈更直觀）。
# ---------------------------------------------------------------------------


def test_soft_threshold_strictly_below_hard_threshold():
    assert config.AUTOPILOT_SUBSYSTEM_MAX < config.AUTOPILOT_SUBSYSTEM_MAX_PENDING, (
        "主動分散軟門檻 AUTOPILOT_SUBSYSTEM_MAX 必須 < 進場硬擋門檻 "
        "AUTOPILOT_SUBSYSTEM_MAX_PENDING，否則 prompt 軟引導比 pre-filter 硬擋更晚觸發，"
        "非對稱雙層治理靜默失效。"
    )


# ---------------------------------------------------------------------------
# 契約守恆再確認：本檔聚焦 done 排除，亦順手釘住 _is_duplicate 字串等值契約不變。
# ---------------------------------------------------------------------------


def test_is_duplicate_string_equality_contract_intact(state):
    backlog.add("唯一 backlog 任務")
    tasks = backlog.list_tasks()
    assert backlog._is_duplicate(tasks, "唯一 backlog 任務") is True  # 字串等值
    assert backlog._is_duplicate(tasks, "唯一 backlog 任務 ") is False  # 非等值（傳入端不 strip）
    assert backlog._is_duplicate(tasks, "另一個任務") is False
