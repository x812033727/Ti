"""任務 #3 / #4 驗收：同義詞 canonical 正規化的正反契約測試。

對應驗收標準 #6 的四類 + ADR 補的第五類（子字串汙染黑樣本）：
  ① 無共享字的同義改寫對：相似度 ≥ ratio 被攔下（#3 閉合 known-limitation 的核心）。
  ② 合法不同/相反意圖任務：詞集高重疊但意圖不同，不被誤殺。
  ③ `backlog._is_duplicate` 字串等值去重契約不變（不經同義正規化），且同一對輸入進
     `_filter_pending_duplicates` 會被攔——兩個對立事實共存，同一測試內並陳避免誤讀。
  ④ `_filter_pending_duplicates` 為純函式：既有任務（existing_titles）不被回溯刪改/mutate。
  ⑤ 子字串汙染黑樣本：含 `add`/`fix` 子串的英文標題（address/prefix/fixture），
     Pass1+2 正規化後 token 不得出現 `add`/`fix` canonical。

純函式比對，不打 LLM/網路/檔案。
"""

from __future__ import annotations

import copy

import pytest

from studio import autopilot, backlog, config

RATIO = config.AUTOPILOT_DEDUP_RATIO  # 0.75，單一常數


# ---------------------------------------------------------------------------
# ① 無共享字同義改寫對：被攔（#3 同義表閉合）
# ---------------------------------------------------------------------------

# (existing, proposal, desc)：兩標題「無共享 ASCII 詞」，靠同義表展開後對齊
_SYNONYM_SHOULD_BLOCK = [
    ("修復去重邏輯", "修正 dedup 邏輯", "修復↔修正 + 去重↔dedup"),
    ("改善 backlog 效能", "優化 backlog 效能", "改善↔優化"),
    ("修復 CI 流程", "fix CI 流程", "修復↔fix（CJK↔ASCII canonical 對齊）"),
    ("替模組補上單元測試", "替模組新增單元測試", "補上↔新增"),
]


@pytest.mark.parametrize("existing,proposal,desc", _SYNONYM_SHOULD_BLOCK)
def test_synonym_rewrite_is_blocked(existing, proposal, desc):
    """同義改寫對相似度應 ≥ ratio 且被 pre-filter 攔下。"""
    sim = autopilot._token_set_similarity(existing, proposal)
    assert sim >= RATIO, f"{desc}: sim={sim:.3f} 應 ≥ {RATIO}"
    assert autopilot._filter_pending_duplicates([proposal], [existing]) == [], desc


# ---------------------------------------------------------------------------
# ② 合法不同 / 相反意圖任務：不誤殺
# ---------------------------------------------------------------------------


def test_opposite_intent_not_misfired():
    """詞集高重疊但意圖相反（新增↔移除）的合法任務不得被擋。"""
    a, b = "新增 backlog 去重測試", "移除 backlog 去重測試"
    sim = autopilot._token_set_similarity(a, b)
    assert sim < RATIO, f"相反意圖 sim={sim:.3f} 不應達閾值"
    assert autopilot._filter_pending_duplicates([a], [b]) == [a]


def test_distinct_subsystem_not_misfired():
    """不同子系統、不同目標的提案零誤殺（即使都含 improve canonical）。"""
    a, b = "改善 backlog 效能", "優化 autopilot 部署流程"
    sim = autopilot._token_set_similarity(a, b)
    assert sim < RATIO, f"異子系統 sim={sim:.3f} 不應達閾值"
    assert autopilot._filter_pending_duplicates([a], [b]) == [a]


# ---------------------------------------------------------------------------
# ③ 字串等值契約不變（雙重事實並陳）
# ---------------------------------------------------------------------------


def test_string_equality_contract_unchanged_yet_prefilter_blocks():
    """backlog 去重維持「字串等值」契約（不經同義正規化），而 pre-filter 仍攔同義對。

    兩個對立但同時成立的事實：
      - `_is_duplicate` 對「修正去重邏輯」vs「修復去重邏輯」回 False（等值合約未被汙染）。
      - 同一對輸入進 `_filter_pending_duplicates` 被攔（同義展開後 Jaccard 達閾值）。
    並陳避免讀者把「③ 回 False」誤判成「系統對同義改寫無防護」。
    """
    tasks = [{"title": "修復去重邏輯", "status": "pending"}]
    # 字串等值：同義不同字 → 非重複（契約不變）
    assert backlog._is_duplicate(tasks, "修正去重邏輯") is False
    # 完全相同字串 → 重複（契約正常）
    assert backlog._is_duplicate(tasks, "修復去重邏輯") is True
    # 但 discovery pre-filter 攔得住同義改寫
    assert autopilot._filter_pending_duplicates(["修正 dedup 邏輯"], ["修復去重邏輯"]) == []


# ---------------------------------------------------------------------------
# ④ 既有任務未被回溯刪改：pre-filter 為純函式，不 mutate 輸入
# ---------------------------------------------------------------------------


def test_existing_titles_not_mutated():
    """`_filter_pending_duplicates` 不得回溯刪改 existing_titles（純函式契約）。"""
    existing = ["修復去重邏輯", "新增 backlog 測試"]
    snapshot = copy.deepcopy(existing)
    proposals = ["修正 dedup 邏輯", "全新無關提案 xyz"]
    out = autopilot._filter_pending_duplicates(proposals, existing)
    # existing 原樣不動
    assert existing == snapshot
    # 同義者被攔、無關者保留
    assert "修正 dedup 邏輯" not in out
    assert "全新無關提案 xyz" in out


# ---------------------------------------------------------------------------
# ⑤ 子字串汙染黑樣本：add/fix 子串不得被誤展開
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "title,forbidden",
    [
        ("address prefix system", {"add", "fix"}),
        ("fixture toolkit refactor", {"fix"}),
        ("paddle gladiator", {"add"}),
    ],
)
def test_no_ascii_substring_contamination(title, forbidden):
    """含 add/fix 子串的合法英文標題，正規化後 token 不得出現 add/fix canonical。"""
    toks = autopilot._tokenize_for_dedup(title)
    assert toks.isdisjoint(forbidden), f"{title} 汙染: {toks & forbidden}"
    # 正向對照：真正的 ASCII 同義詞 token 仍精確映射（證明非整體關閉）
    assert autopilot._tokenize_for_dedup("fixes adding").issuperset({"fix", "add"})
