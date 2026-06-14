"""任務 #2 驗收：同義改寫樣本集——新詞集 Jaccard 相較舊 SequenceMatcher 多攔住標註「應視為
重複」者；擋不住的改寫以 known-limitation 誠實標示，不假綠。

設計（呼應 DECISIONS）：
- 主防線是詞集 Jaccard（與語序無關），閾值維持單一常數 `AUTOPILOT_DEDUP_RATIO = 0.75`。
- 不調到 0.55：實測 0.55 對下列「應重複」樣本零新增命中，反而會誤殺「相反意圖但詞集高重疊」
  哨兵（Jaccard≈0.556），故維持 0.75。哨兵測試見 `test_opposite_intent_not_misfired`。
- 純 stdlib，不引入 jieba（CJK 逐字成 token 已足以攔語序/共享字根改寫）。

純函式比對，不打 LLM/網路/檔案。
"""

from __future__ import annotations

import difflib

import pytest

from studio import autopilot, config

THRESHOLD = config.AUTOPILOT_DEDUP_RATIO  # 0.75，單一常數


def _old_seqmatcher(a: str, b: str) -> float:
    """舊策略：difflib.SequenceMatcher 字元序列比對（已被取代，僅供對照）。"""
    norm = autopilot._normalize_for_dedup
    return difflib.SequenceMatcher(None, norm(a), norm(b)).ratio()


def _new_jaccard(a: str, b: str) -> float:
    return autopilot._token_set_similarity(a, b)


# ---------------------------------------------------------------------------
# ≥3 組同義改寫樣本（標註「應視為重複」），逐一對照舊/新策略
# ---------------------------------------------------------------------------

# (existing 標題, 提案改寫, 說明)
_SHOULD_BE_DUP = [
    ("為 retry 機制加上重試上限", "為重試機制加上 retry 上限", "中英混排語序調換"),
    ("修復 CI 的 merge 流程", "merge 流程的 CI 修復", "純語序顛倒"),
    ("替 backlog 模組補上單元測試", "替 backlog 模組補上單測", "縮寫"),
    ("修復登入逾時的重試邏輯", "修正登入逾時的重試邏輯", "同義詞替換（共享多數字）"),
]


@pytest.mark.parametrize("existing,proposal,desc", _SHOULD_BE_DUP)
def test_new_strategy_blocks_should_be_dup(existing, proposal, desc):
    """新詞集策略應攔下所有標註「應視為重複」的改寫。"""
    assert _new_jaccard(proposal, existing) >= THRESHOLD, desc
    assert autopilot._filter_pending_duplicates([proposal], [existing]) == []


def test_new_catches_strictly_more_than_old():
    """驗收核心：新策略至少多攔住一組舊 SequenceMatcher 在同一 0.75 閾值下漏網的改寫。

    具判別力地列出每組舊/新命中，並斷言「新攔下的真子集 ⊋ 舊攔下的」。
    """
    old_caught, new_caught = set(), set()
    for existing, proposal, desc in _SHOULD_BE_DUP:
        if _old_seqmatcher(proposal, existing) >= THRESHOLD:
            old_caught.add(desc)
        if _new_jaccard(proposal, existing) >= THRESHOLD:
            new_caught.add(desc)
    # 新策略攔下全部應重複樣本；舊策略至少漏掉語序類
    assert new_caught == {d for _, _, d in _SHOULD_BE_DUP}
    assert old_caught < new_caught, f"舊={old_caught} 應為新={new_caught} 的真子集"
    # 明確點名舊策略漏掉的（語序類），證明新增防護來源
    assert "純語序顛倒" not in old_caught
    assert "純語序顛倒" in new_caught


# ---------------------------------------------------------------------------
# 判別力：黑樣本 / 相反意圖哨兵——不該誤殺
# ---------------------------------------------------------------------------


def test_opposite_intent_not_misfired():
    """反向哨兵：詞集高重疊但意圖相反的「合法不同任務」不得被擋。

    「新增 backlog 去重測試」↔「移除 backlog 去重測試」Jaccard≈0.556：
    - 0.75 閾值下正確放行（保留判別力）；
    - 若把閾值調到 0.55（架構討論中的備案），此處會被誤殺——以此釘住「不採 0.55」的代價。
    """
    a, b = "新增 backlog 去重測試", "移除 backlog 去重測試"
    r = _new_jaccard(a, b)
    assert 0.5 < r < 0.75, f"哨兵 Jaccard={r:.3f} 偏離預期區間"
    assert autopilot._filter_pending_duplicates([a], [b]) == [a]  # 0.75 下放行
    # 釘住代價：閾值若降到 0.55，這組相反意圖任務會被誤殺
    assert r >= 0.55


def test_distinct_subsystems_not_misfired():
    """異子系統、方向不同的提案零誤殺（黑樣本）。"""
    a, b = "改善 backlog 效能", "優化 autopilot 效率"
    assert _new_jaccard(a, b) < THRESHOLD
    assert autopilot._filter_pending_duplicates([a], [b]) == [a]


# ---------------------------------------------------------------------------
# Known-limitation：誠實標示詞集策略仍擋不住的同義替換，不假綠
# ---------------------------------------------------------------------------

# 無共享字根的純同義替換——字級分詞無法辨識，stdlib 方案的真實天花板。
_KNOWN_LIMITATION = [
    ("補測試", "新增測試", "補↔新增：同義動詞無共享字"),
    ("優化 backlog 效能", "改善 backlog 效率", "優化↔改善 且 效能↔效率：雙重同義替換"),
]


@pytest.mark.parametrize("existing,proposal,desc", _KNOWN_LIMITATION)
def test_known_limitation_synonym_substitution_slips(existing, proposal, desc):
    """誠實標示：無共享字根的同義替換，詞集 Jaccard 仍低於閾值而漏網。

    這是 stdlib（不引入 jieba/詞向量）的已知天花板，非期望行為。釘住「確實 < 閾值」+「確實漏網」，
    使代價在 CI 永遠可見；補位靠 prompt 負向指令與 #3 子系統覆蓋 pre-filter，非本層硬擋。
    """
    assert _new_jaccard(proposal, existing) < THRESHOLD, f"{desc}：若已能攔則應移出 known-limitation"
    assert autopilot._filter_pending_duplicates([proposal], [existing]) == [proposal]
