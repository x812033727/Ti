"""任務 #2 驗收測試：`_evaluate_self` 提案進場的 difflib 相似度 pre-filter。

純檔案 IO + monkeypatch，不打 LLM/網路。涵蓋：
- pre-filter 對高重疊提案歸零、對黑樣本零誤殺；
- 比對範圍涵蓋 pending + in_progress（與 prompt 禁止清單對齊）；
- 閾值集中為單一模組常數（0.55，邊緣補強），可調整（測試以 monkeypatch 模擬）；
- 降閾值多攔回「極短同義標題」，並以反向哨兵誠實標註其 false-positive 代價；
- 端到端 `_evaluate_self`（mock Expert.speak）下高重疊提案實際進場數為 0；
- `_is_duplicate` 字串等值契約未被改動、既有任務不被刪除。

風格對齊 tests/autopilot/test_autopilot_eval_memory.py（state fixture 指向 tmp）。
"""

from __future__ import annotations

import difflib
import inspect

import pytest

from studio import autopilot, backlog, config


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_STATE_DIR", tmp_path / "ap")
    monkeypatch.setattr(config, "AUTOPILOT_EVAL_MEMORY", 20)
    return tmp_path


# ---------------------------------------------------------------------------
# 驗收 4：閾值集中為單一模組常數、零新外部依賴
# ---------------------------------------------------------------------------


def test_threshold_is_single_module_constant():
    assert hasattr(config, "AUTOPILOT_DEDUP_RATIO")
    assert isinstance(config.AUTOPILOT_DEDUP_RATIO, float)
    # 單一可調常數：0.55（邊緣補強，主防線是子系統覆蓋計數器，另案）。
    assert config.AUTOPILOT_DEDUP_RATIO == pytest.approx(0.55)


def test_uses_stdlib_difflib_no_extra_dep():
    # 用 stdlib difflib，無第三方相似度依賴（rapidfuzz 等）。
    src = inspect.getsource(autopilot._filter_pending_duplicates)
    assert "SequenceMatcher" in src
    assert "rapidfuzz" not in src.lower() and "fuzzywuzzy" not in src.lower()


# ---------------------------------------------------------------------------
# 驗收 2/3：高重疊歸零、黑樣本零誤殺、空清單不過濾
# ---------------------------------------------------------------------------


def test_high_overlap_filtered_to_zero(monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_DEDUP_RATIO", 0.55)
    existing = ["修復登入逾時的重試邏輯", "替 backlog 模組補上單元測試"]
    proposals = [
        "修正登入逾時的重試邏輯",  # 同義詞替換，實測 ratio 0.909
        "替 backlog 模組補上單測",  # 縮寫，實測 ratio 0.941
    ]
    kept = autopilot._filter_pending_duplicates(proposals, existing)
    assert kept == []


def test_distinct_topics_all_kept(monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_DEDUP_RATIO", 0.55)
    existing = ["修復登入逾時的重試邏輯"]
    proposals = ["重構前端首頁的載入動畫", "為設定檔加上 schema 驗證", "撰寫部署腳本的回滾流程"]
    kept = autopilot._filter_pending_duplicates(proposals, existing)
    assert kept == proposals  # 黑樣本：完全不同主題零誤殺


def test_mixed_keeps_only_non_overlapping(monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_DEDUP_RATIO", 0.55)
    existing = ["修復登入逾時的重試邏輯"]
    proposals = ["修正登入逾時的重試邏輯", "為設定檔加上 schema 驗證"]
    kept = autopilot._filter_pending_duplicates(proposals, existing)
    assert kept == ["為設定檔加上 schema 驗證"]


def test_empty_existing_returns_all_unchanged(monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_DEDUP_RATIO", 0.55)
    proposals = ["任務 A", "任務 B"]
    assert autopilot._filter_pending_duplicates(proposals, []) == proposals


def test_threshold_is_adjustable(monkeypatch):
    # 單一常數可調：放寬到 0.99 時，同義改寫（ratio 0.909）不再被擋；回到預設 0.55 則擋住。
    existing = ["修復登入逾時的重試邏輯"]
    proposals = ["修正登入逾時的重試邏輯"]
    monkeypatch.setattr(config, "AUTOPILOT_DEDUP_RATIO", 0.99)
    assert autopilot._filter_pending_duplicates(proposals, existing) == proposals
    monkeypatch.setattr(config, "AUTOPILOT_DEDUP_RATIO", 0.55)
    assert autopilot._filter_pending_duplicates(proposals, existing) == []


# ---------------------------------------------------------------------------
# 邊界案例溯源（DECISIONS 要求）：實測 ratio 釘住閾值行為
# ---------------------------------------------------------------------------


def test_boundary_ratios_documented():
    # 釘住「實測 ratio vs 現行單一常數」的關係，避免日後調 threshold 時無聲改變判別。
    thr = config.AUTOPILOT_DEDUP_RATIO

    def norm(s):
        return autopilot._normalize_for_dedup(s)

    cases = [
        ("修復登入逾時的重試邏輯", "修正登入逾時的重試邏輯", 0.90, True),  # 同義詞 → 擋
        ("替 backlog 模組補上單元測試", "替 backlog 模組補上單測", 0.90, True),  # 縮寫 → 擋
        ("修復登入逾時的重試邏輯", "重構前端首頁的載入動畫", 0.20, False),  # 黑樣本 → 放行
    ]
    for a, b, lo, should_block in cases:
        r = difflib.SequenceMatcher(None, norm(a), norm(b)).ratio()
        assert (r >= thr) == should_block, f"{a!r}<>{b!r} ratio={r:.3f} thr={thr}"
        if should_block:
            assert r >= lo
        else:
            assert r <= lo


# ---------------------------------------------------------------------------
# 驗收 2：降閾值（0.75→0.55）相較舊策略「至少多攔住一個應視為重複的同義改寫」
# ---------------------------------------------------------------------------


def test_lower_threshold_catches_short_synonym(monkeypatch):
    # 正向證據：極短同義標題「補測試」↔「新增測試」(實測 ratio≈0.571) 在舊 0.75 漏網、
    # 新 0.55 攔住。這就是降閾值的增量價值（多撈回過去滑溜的短同義提案）。
    existing = ["補測試"]
    proposals = ["新增測試"]
    r = difflib.SequenceMatcher(
        None,
        autopilot._normalize_for_dedup(proposals[0]),
        autopilot._normalize_for_dedup(existing[0]),
    ).ratio()
    assert 0.55 <= r < 0.75, f"ratio={r:.3f} 須落在 0.55~0.75 之間才證明增量"
    monkeypatch.setattr(config, "AUTOPILOT_DEDUP_RATIO", 0.75)
    assert autopilot._filter_pending_duplicates(proposals, existing) == proposals  # 舊閾值漏
    monkeypatch.setattr(config, "AUTOPILOT_DEDUP_RATIO", 0.55)
    assert autopilot._filter_pending_duplicates(proposals, existing) == []  # 新閾值攔


def test_reverse_sentinel_false_positive_cost(monkeypatch):
    # 反向哨兵（誠實標註代價，非假綠）：0.55 下「提高重試上限」↔「降低重試上限」(ratio≈0.667)
    # 語意相反卻被當重複擋掉——這是降閾值新引入的 false-positive，且在舊 0.75 下不會發生。
    # 釘住此誤殺以確保 CI 永遠看得到代價，而非假裝硬擋無副作用。
    existing = ["提高重試上限"]
    proposals = ["降低重試上限"]  # 方向相反，理應視為不同任務
    r = difflib.SequenceMatcher(
        None,
        autopilot._normalize_for_dedup(proposals[0]),
        autopilot._normalize_for_dedup(existing[0]),
    ).ratio()
    assert 0.55 <= r < 0.75, f"ratio={r:.3f} 須落在 0.55~0.75 才是降閾值獨有的誤殺"
    monkeypatch.setattr(config, "AUTOPILOT_DEDUP_RATIO", 0.75)
    assert autopilot._filter_pending_duplicates(proposals, existing) == proposals  # 舊閾值不誤殺
    monkeypatch.setattr(config, "AUTOPILOT_DEDUP_RATIO", 0.55)
    assert autopilot._filter_pending_duplicates(proposals, existing) == []  # 新閾值誤殺（已知代價）


def test_known_limitation_pure_synonym_still_slips(monkeypatch):
    # 已知限制（誠實，非假綠）：純同義替換且字元重疊低者，字元級比對連 0.55 都擋不住。
    # 「撰寫部署腳本」↔「編寫發佈腳本」(撰/編、部署/發佈 全替換, ratio≈0.500) 仍漏網。
    # 要擋這類需詞集/分詞（jieba），已由子題 2 裁定「不引入新依賴」——故記錄為缺口，
    # 靠 prompt 負向指令與子系統計數器（另案）補位，非本層硬擋。
    monkeypatch.setattr(config, "AUTOPILOT_DEDUP_RATIO", 0.55)
    existing = ["撰寫部署腳本"]
    proposals = ["編寫發佈腳本"]
    r = difflib.SequenceMatcher(
        None,
        autopilot._normalize_for_dedup(proposals[0]),
        autopilot._normalize_for_dedup(existing[0]),
    ).ratio()
    assert r < 0.55  # 釘住：確實低於閾值（記錄缺口，非期望行為）
    assert autopilot._filter_pending_duplicates(proposals, existing) == proposals  # 漏網


# ---------------------------------------------------------------------------
# 驗收 3：比對範圍涵蓋 in_progress（與 prompt 禁止清單對齊）
# ---------------------------------------------------------------------------


def test_filter_covers_in_progress(state):
    t = backlog.add("優化資料庫查詢的索引")
    backlog.set_status(t["id"], "in_progress")
    titles = autopilot._pending_titles()
    assert "優化資料庫查詢的索引" in titles  # in_progress 也納入清單
    kept = autopilot._filter_pending_duplicates(["優化資料庫查詢之索引"], titles)
    assert kept == []  # 與 in_progress 高相似的提案被擋


# ---------------------------------------------------------------------------
# 驗收 2/3 端到端：mock Expert.speak，高重疊提案實際進場數為 0
# ---------------------------------------------------------------------------


class _FakeExpert:
    """替身：speak 回傳預設文字，避免任何 LLM/網路。"""

    _reply = ""

    def __init__(self, *a, **k):
        pass

    async def speak(self, prompt, on_event):
        type(self).last_prompt = prompt
        return self._reply

    async def stop(self):
        return None


def _patch_expert(monkeypatch, reply: str):
    import studio.experts as experts_mod

    _FakeExpert._reply = reply
    monkeypatch.setattr(experts_mod, "Expert", _FakeExpert)


@pytest.mark.asyncio
async def test_evaluate_self_blocks_overlapping_proposals(state, monkeypatch):
    seed = backlog.add("修復登入逾時的重試邏輯")
    backlog.set_status(seed["id"], "pending")
    before = len(backlog.list_tasks("pending"))

    reply = "\n".join(
        [
            "任務: 修正登入逾時的重試邏輯",  # 與 pending 高相似 → 應被擋
            "任務: 為設定檔加上 schema 驗證",  # 不重疊 → 應進場
        ]
    )
    _patch_expert(monkeypatch, reply)

    n = await autopilot._evaluate_self(str(state))
    assert n == 1  # 只有非重疊者進場
    pendings = [t["title"] for t in backlog.list_tasks("pending")]
    assert "為設定檔加上 schema 驗證" in pendings
    assert "修正登入逾時的重試邏輯" not in pendings  # 重疊提案未進場
    assert len(backlog.list_tasks("pending")) == before + 1


@pytest.mark.asyncio
async def test_evaluate_self_all_overlap_yields_zero(state, monkeypatch):
    seed = backlog.add("替 backlog 模組補上單元測試")
    backlog.set_status(seed["id"], "pending")

    reply = "任務: 替 backlog 模組補上單測"  # ratio 0.941 → 全擋
    _patch_expert(monkeypatch, reply)

    n = await autopilot._evaluate_self(str(state))
    assert n == 0  # 進場數為 0


# ---------------------------------------------------------------------------
# 驗收 3：_is_duplicate 字串等值契約未被改動、既有任務不被刪除
# ---------------------------------------------------------------------------


def test_is_duplicate_contract_unchanged(state):
    backlog.add("唯一任務 A")
    tasks = backlog.list_tasks()
    # 字串等值：相同標題視為重複
    assert backlog._is_duplicate(tasks, "唯一任務 A") is True
    # 高相似但不等值：_is_duplicate 不該攔（語意去重不在它職責）
    assert backlog._is_duplicate(tasks, "唯一任務 Ａ") is False
    assert backlog._is_duplicate(tasks, "唯一任務 B") is False


@pytest.mark.asyncio
async def test_prefilter_does_not_delete_existing(state, monkeypatch):
    a = backlog.add("既有任務一")
    b = backlog.add("既有任務二")
    backlog.set_status(a["id"], "pending")
    backlog.set_status(b["id"], "pending")
    before_ids = {t["id"] for t in backlog.list_tasks()}

    _patch_expert(monkeypatch, "任務: 既有任務一")  # 與既有高度重疊
    await autopilot._evaluate_self(str(state))

    after_ids = {t["id"] for t in backlog.list_tasks()}
    assert before_ids <= after_ids  # 既有任務一個都沒少（pre-filter 不回溯清洗）
