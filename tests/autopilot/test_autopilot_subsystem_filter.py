"""任務 #3 驗收測試：`_filter_pending_duplicates` 的第二道「子系統覆蓋廣度」pre-filter。

純檔案 IO + monkeypatch，不打 LLM/網路。涵蓋：
- 子系統抽取（_extract_subsystems）：CJK 子字串命中、英文 \b 邊界零誤命中、IGNORECASE；
- 覆蓋計數（_count_subsystem_coverage）回傳 Counter、同標題多子系統各計一次；
- K-filter：同子系統 pending 達 K 筆時新提案被拒；
- 反向證明：既有任務未被刪除、`backlog._is_duplicate` 契約不變、不同子系統提案不受影響、
  未達 K 的子系統不受影響；
- 端到端 `_evaluate_self`（mock Expert.speak）下子系統過多的提案實際進場數受限。

風格對齊 tests/autopilot/test_autopilot_prefilter.py（state fixture 指向 tmp）。
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
# K 為單一模組常數、零新外部依賴
# ---------------------------------------------------------------------------


def test_k_is_single_module_constant():
    assert hasattr(config, "AUTOPILOT_SUBSYSTEM_MAX_PENDING")
    assert isinstance(config.AUTOPILOT_SUBSYSTEM_MAX_PENDING, int)
    assert config.AUTOPILOT_SUBSYSTEM_MAX_PENDING >= 1


def test_no_extra_dependency():
    # 純 stdlib（re + collections.Counter），無第三方分詞/相似度依賴。
    src = inspect.getsource(autopilot._filter_pending_duplicates)
    src += inspect.getsource(autopilot._count_subsystem_coverage)
    for bad in ("jieba", "rapidfuzz", "fuzzywuzzy", "sklearn", "numpy"):
        assert bad not in src.lower()


# ---------------------------------------------------------------------------
# _extract_subsystems：CJK 子字串命中、英文 \b 零誤命中、IGNORECASE
# ---------------------------------------------------------------------------


def test_extract_cjk_substring_hits():
    # CJK 詞嵌在連續中文標題中也要命中（lookahead/\b 在此會漏抓，故用純子字串）。
    assert autopilot._extract_subsystems("改善去重邏輯效能") == {"去重"}
    assert autopilot._extract_subsystems("強化提案去重") == {"去重"}
    assert autopilot._extract_subsystems("重構評估流程") == {"評估"}


def test_extract_english_word_boundary_no_false_hit():
    # \b 邊界：ci 不命中 social、merge 不命中 emergence、decide 不誤觸。
    assert autopilot._extract_subsystems("social interaction 重構") == set()
    assert autopilot._extract_subsystems("emergence 行為觀測") == set()
    assert autopilot._extract_subsystems("decide 分流邏輯") == set()
    # 真正獨立詞要命中。
    assert "ci" in autopilot._extract_subsystems("修復 CI 綠燈")
    assert "merge" in autopilot._extract_subsystems("處理 merge 衝突")


def test_extract_ignorecase_and_plural():
    assert autopilot._extract_subsystems("重構 Backlog 載入") == {"backlog"}
    assert autopilot._extract_subsystems("補 expert 測試") == {"experts"}
    assert autopilot._extract_subsystems("補 experts 測試") == {"experts"}


def test_extract_multiple_subsystems_in_one_title():
    got = autopilot._extract_subsystems("讓 backlog 與 discovery 對齊去重")
    assert got == {"backlog", "discovery", "去重"}


def test_extract_no_match_returns_empty():
    assert autopilot._extract_subsystems("為設定檔加上 schema 驗證") == set()


# ---------------------------------------------------------------------------
# _count_subsystem_coverage：Counter、多子系統各計一次
# ---------------------------------------------------------------------------


def test_count_returns_counter():
    from collections import Counter

    cov = autopilot._count_subsystem_coverage(["重構 backlog", "優化 backlog 查詢", "改善去重"])
    assert isinstance(cov, Counter)
    assert cov["backlog"] == 2
    assert cov["去重"] == 1
    assert cov["不存在子系統"] == 0  # Counter 缺鍵回 0


def test_count_multi_subsystem_title_counts_each():
    cov = autopilot._count_subsystem_coverage(["backlog 與 discovery 同步"])
    assert cov["backlog"] == 1 and cov["discovery"] == 1


# ---------------------------------------------------------------------------
# K-filter 正向：同子系統 pending 達 K 筆時新提案被拒
# ---------------------------------------------------------------------------


def test_proposal_rejected_when_subsystem_at_k(monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_SUBSYSTEM_MAX_PENDING", 2)
    existing = ["重構 backlog 載入", "優化 backlog 查詢"]  # backlog 已達 2
    proposals = ["再為 backlog 加快照"]  # 同子系統 → 應被拒
    assert autopilot._filter_pending_duplicates(proposals, existing) == []


def test_proposal_kept_when_subsystem_below_k(monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_SUBSYSTEM_MAX_PENDING", 3)
    existing = ["重構 backlog 載入", "優化 backlog 查詢"]  # backlog=2 < 3
    proposals = ["再為 backlog 加快照"]
    assert autopilot._filter_pending_duplicates(proposals, existing) == proposals


def test_intra_batch_accumulation_caps_same_subsystem(monkeypatch):
    # existing 無 backlog，但同一批 3 個 backlog 提案在 K=2 下只放行前 2 個。
    monkeypatch.setattr(config, "AUTOPILOT_SUBSYSTEM_MAX_PENDING", 2)
    existing = ["不相關的任務標題"]
    proposals = ["backlog 任務一", "backlog 任務二", "backlog 任務三"]
    kept = autopilot._filter_pending_duplicates(proposals, existing)
    assert kept == ["backlog 任務一", "backlog 任務二"]


# ---------------------------------------------------------------------------
# 反向：不同子系統提案不受影響、未命中任何子系統者不受影響
# ---------------------------------------------------------------------------


def test_other_subsystem_not_affected(monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_SUBSYSTEM_MAX_PENDING", 2)
    existing = ["重構 backlog 載入", "優化 backlog 查詢"]  # 只有 backlog 滿
    proposals = ["補 discovery 測試", "改善 runner 重啟", "為設定檔加上 schema 驗證"]
    # 不同子系統 + 無子系統 → 全數保留
    assert autopilot._filter_pending_duplicates(proposals, existing) == proposals


def test_unmatched_proposals_never_blocked_by_k(monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_SUBSYSTEM_MAX_PENDING", 1)
    existing = ["backlog A", "discovery B", "merge C"]  # 多子系統各 1，K=1 全滿
    proposals = ["寫一份新的設計說明", "調整前端樣式"]  # 不觸任何子系統
    assert autopilot._filter_pending_duplicates(proposals, existing) == proposals


def test_empty_existing_returns_all(monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_SUBSYSTEM_MAX_PENDING", 1)
    proposals = ["backlog X", "backlog Y"]
    # existing 空 → 無「已過多」子系統，原樣返回（與第一道一致的早退語意）。
    assert autopilot._filter_pending_duplicates(proposals, []) == proposals


# ---------------------------------------------------------------------------
# 反向：既有任務未被刪除、_is_duplicate 契約不變
# ---------------------------------------------------------------------------


def test_is_duplicate_contract_unchanged(state):
    backlog.add("重構 backlog 載入")
    tasks = backlog.list_tasks()
    # 字串等值：相同標題視為重複；不同/高相似但不等值：不攔（語意去重不在它職責）。
    assert backlog._is_duplicate(tasks, "重構 backlog 載入") is True
    assert backlog._is_duplicate(tasks, "重構 backlog 快取") is False
    assert backlog._is_duplicate(tasks, "優化 backlog 查詢") is False


def _patch_expert(monkeypatch, reply: str):
    import studio.experts as experts_mod

    class _FakeExpert:
        def __init__(self, *a, **k):
            pass

        async def speak(self, prompt, on_event):
            type(self).last_prompt = prompt
            return reply

        async def stop(self):
            return None

    monkeypatch.setattr(experts_mod, "Expert", _FakeExpert)


@pytest.mark.asyncio
async def test_k_filter_does_not_delete_existing(state, monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_SUBSYSTEM_MAX_PENDING", 2)
    a = backlog.add("重構 backlog 載入")
    b = backlog.add("優化 backlog 查詢")
    backlog.set_status(a["id"], "pending")
    backlog.set_status(b["id"], "pending")
    before_ids = {t["id"] for t in backlog.list_tasks()}

    _patch_expert(monkeypatch, "任務: 再為 backlog 加一層快取")  # 同子系統、應被擋
    n = await autopilot._evaluate_self(str(state))

    assert n == 0  # 子系統已滿 → 進場數為 0
    after_ids = {t["id"] for t in backlog.list_tasks()}
    assert before_ids <= after_ids  # 既有任務一個都沒少（pre-filter 不回溯刪改）


@pytest.mark.asyncio
async def test_evaluate_self_keeps_breadth_proposals(state, monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_SUBSYSTEM_MAX_PENDING", 2)
    for title in ("重構 backlog 載入", "優化 backlog 查詢"):
        t = backlog.add(title)
        backlog.set_status(t["id"], "pending")

    reply = "\n".join(
        [
            "任務: 再為 backlog 加快照",  # backlog 已滿 → 擋
            "任務: 補 discovery 模組測試",  # 不同子系統 → 進場
        ]
    )
    _patch_expert(monkeypatch, reply)
    n = await autopilot._evaluate_self(str(state))

    assert n == 1
    pendings = [t["title"] for t in backlog.list_tasks("pending")]
    assert "補 discovery 模組測試" in pendings
    assert "再為 backlog 加快照" not in pendings
