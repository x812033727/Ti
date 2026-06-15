"""QA 破壞性驗證（任務 #4 / 子題：同主題隧道防線）。

獨立於工程師自帶測試，從「預設東西是壞的」出發，補強驗收標準的反向證明與契約邊界：
- K-filter off-by-one：恰 K-1 放行、恰 K 拒；intra-batch 跨越 K 的精確切點。
- 反向證明（驗收三要點）：既有任務未被刪除、`backlog._is_duplicate` 契約不變、不同子系統不受影響。
- 純函式副作用零：`_filter_pending_duplicates` 不得 mutate 傳入 list、不得寫 backlog state。
- `_is_duplicate` 契約全狀態覆蓋：done 不擋、in_progress 擋、strip 語意、簽名穩定。
- 判別力（排除假綠）：放大 K 後原本被擋者必須放行，證明是 K 在起作用而非他因。
- 第一/二道防線交互：相似度先擋的提案不得污染子系統 coverage 計數。
"""

from __future__ import annotations

import inspect

import pytest

from studio import autopilot, backlog, config


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_STATE_DIR", tmp_path / "ap")
    monkeypatch.setattr(config, "AUTOPILOT_EVAL_MEMORY", 20)
    return tmp_path


# ---------------------------------------------------------------------------
# K off-by-one：達 K 才拒（>=K），K-1 不拒
# ---------------------------------------------------------------------------


def test_k_minus_one_kept_exactly_k_rejected(monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_SUBSYSTEM_MAX_PENDING", 3)
    # backlog=2 (K-1) → 新提案放行（放行後變 3）
    e2 = ["重構 backlog 載入", "優化 backlog 查詢"]
    assert autopilot._filter_pending_duplicates(["backlog 加快照"], e2) == ["backlog 加快照"]
    # backlog=3 (=K) → 新提案被拒
    e3 = e2 + ["backlog 壓縮"]
    assert autopilot._filter_pending_duplicates(["backlog 加快照"], e3) == []


def test_intra_batch_exact_cutpoint(monkeypatch):
    # existing backlog=1，K=3：本批可再放 2 個（累加到 3），第 3 個起被擋。
    monkeypatch.setattr(config, "AUTOPILOT_SUBSYSTEM_MAX_PENDING", 3)
    existing = ["重構 backlog 載入"]
    proposals = ["backlog 甲", "backlog 乙", "backlog 丙", "backlog 丁"]
    kept = autopilot._filter_pending_duplicates(proposals, existing)
    assert kept == ["backlog 甲", "backlog 乙"]


# ---------------------------------------------------------------------------
# 判別力：排除假綠——放大 K，原本被擋者必須全部放行
# ---------------------------------------------------------------------------


def test_discriminating_power_large_k_admits_all(monkeypatch):
    existing = ["重構 backlog 載入", "優化 backlog 查詢", "backlog 壓縮"]
    proposals = ["backlog 新功能甲", "backlog 新功能乙"]
    monkeypatch.setattr(config, "AUTOPILOT_SUBSYSTEM_MAX_PENDING", 2)
    assert autopilot._filter_pending_duplicates(proposals, existing) == []  # 小 K：擋
    monkeypatch.setattr(config, "AUTOPILOT_SUBSYSTEM_MAX_PENDING", 999)
    assert autopilot._filter_pending_duplicates(proposals, existing) == proposals  # 大 K：放行


# ---------------------------------------------------------------------------
# 純函式副作用：不得 mutate 傳入 list、不得寫 backlog state
# ---------------------------------------------------------------------------


def test_inputs_not_mutated(monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_SUBSYSTEM_MAX_PENDING", 1)
    proposals = ["backlog 甲", "discovery 乙"]
    existing = ["backlog 既有"]
    prop_copy, exist_copy = list(proposals), list(existing)
    autopilot._filter_pending_duplicates(proposals, existing)
    assert proposals == prop_copy  # 傳入提案 list 不被原地改動
    assert existing == exist_copy  # 傳入 existing list 不被原地改動


def test_filter_does_not_touch_backlog_state(state, monkeypatch):
    # 呼叫純 filter 後 backlog 不應憑空多出/少掉任務（它不負責落盤）。
    monkeypatch.setattr(config, "AUTOPILOT_SUBSYSTEM_MAX_PENDING", 1)
    backlog.add("重構 backlog 載入")
    before = [t["id"] for t in backlog.list_tasks()]
    autopilot._filter_pending_duplicates(
        ["backlog 再加一個", "discovery 全新"], ["重構 backlog 載入"]
    )
    after = [t["id"] for t in backlog.list_tasks()]
    assert before == after


# ---------------------------------------------------------------------------
# backlog._is_duplicate 契約：全狀態覆蓋（done 不擋、in_progress 擋、strip）
# ---------------------------------------------------------------------------


def test_is_duplicate_signature_stable():
    sig = inspect.signature(backlog._is_duplicate)
    # 契約：(tasks, title) → bool；多/少參數都算破壞契約。
    assert list(sig.parameters) == ["tasks", "title"]


def test_is_duplicate_done_does_not_block():
    # 契約核心：只擋 pending/in_progress；done 不視為重複（否則同名後續任務永遠進不來）。
    tasks = [{"title": "重構 backlog 載入", "status": "done"}]
    assert backlog._is_duplicate(tasks, "重構 backlog 載入") is False


def test_is_duplicate_pending_and_in_progress_block():
    tasks = [
        {"title": "甲任務", "status": "pending"},
        {"title": "乙任務", "status": "in_progress"},
    ]
    assert backlog._is_duplicate(tasks, "甲任務") is True
    assert backlog._is_duplicate(tasks, "乙任務") is True


def test_is_duplicate_strip_is_asymmetric():
    # 契約鎖定：strip 只作用於「既有 task 的 title」，傳入的 title 參數不 strip。
    # （此非對稱性是現況契約；反向證明「契約不變」須把它釘死，避免日後悄悄改成雙邊 strip。）
    tasks_dirty = [{"title": "  重構 backlog 載入  ", "status": "pending"}]
    assert backlog._is_duplicate(tasks_dirty, "重構 backlog 載入") is True  # task 端 strip → 命中
    tasks_clean = [{"title": "重構 backlog 載入", "status": "pending"}]
    assert backlog._is_duplicate(tasks_clean, "  重構 backlog 載入  ") is False  # 傳入端不 strip


def test_is_duplicate_distinct_title_not_blocked():
    tasks = [{"title": "重構 backlog 載入", "status": "pending"}]
    assert backlog._is_duplicate(tasks, "重構 backlog 快取") is False  # 高相似但不等值：不歸它管


# ---------------------------------------------------------------------------
# 第一/二道防線交互：相似度先擋者不得污染 coverage
# ---------------------------------------------------------------------------


def test_similarity_drop_does_not_pollute_coverage(monkeypatch):
    # K=2、existing backlog=1。第一個提案與 existing 幾乎等值 → 第一道相似度擋掉，
    # 不應計入 coverage；故第二個 backlog 提案仍能放行（coverage 1→2 才達 K）。
    monkeypatch.setattr(config, "AUTOPILOT_SUBSYSTEM_MAX_PENDING", 2)
    monkeypatch.setattr(config, "AUTOPILOT_DEDUP_RATIO", 0.9)
    existing = ["重構 backlog 載入"]
    proposals = ["重構 backlog 載入", "backlog 全新快照機制"]  # 第一個是近重複
    kept = autopilot._filter_pending_duplicates(proposals, existing)
    assert kept == ["backlog 全新快照機制"]


# ---------------------------------------------------------------------------
# 端到端反向：K-filter 擋下提案時，既有任務一筆不少、狀態不變
# ---------------------------------------------------------------------------


def _patch_expert(monkeypatch, reply: str):
    import studio.experts as experts_mod

    class _FakeExpert:
        def __init__(self, *a, **k):
            pass

        async def speak(self, prompt, on_event):
            return reply

        async def stop(self):
            return None

    monkeypatch.setattr(experts_mod, "Expert", _FakeExpert)


@pytest.mark.asyncio
async def test_e2e_blocked_proposal_preserves_existing_status(state, monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_SUBSYSTEM_MAX_PENDING", 2)
    a = backlog.add("重構 backlog 載入")
    b = backlog.add("優化 backlog 查詢")
    backlog.set_status(a["id"], "pending")
    backlog.set_status(b["id"], "pending")
    snapshot = {t["id"]: t["status"] for t in backlog.list_tasks()}

    _patch_expert(monkeypatch, "任務: 再為 backlog 疊一層快取")  # 同子系統、應被擋
    n = await autopilot._evaluate_self(str(state))

    assert n == 0
    after = {t["id"]: t["status"] for t in backlog.list_tasks()}
    assert after == snapshot  # 既有任務的 id 與 status 全數原封不動
