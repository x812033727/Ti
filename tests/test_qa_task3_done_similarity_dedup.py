"""任務 #3 黑白樣本守門：done-list 相似度去重的契約與端到端行為。

補在 `tests/test_improver_discover_done_dedup.py`（任務 #2 伴隨測試）之外，聚焦：

1. **代表性改寫被擋（非退化）**：黑樣本用「換詞改寫」——把 done 標題的動詞「改善」換成「調整」
   （不同用詞、非同義詞收斂、非搬字序），詞集 Jaccard≈0.79 落在門檻(0.75)與 1.0 之間，證明相似層
   攔的是真正的換句話說，而不只是 Jaccard=1.0 的語序重排退化案例。
2. **單一來源契約**：done 與 pending 兩防線共用 `autopilot._first_similar_title`——同一黑樣本在
   pending 的 `_filter_pending_duplicates` 亦被丟棄，判定一致。
3. **端到端 dropped 回報**（驗收 #6）：done 相似層新擋下的項目要計入 `_discover` 廣播的
   「源頭擋掉 N 個」訊息，不靜默丟棄。
4. **開關向後相容**（驗收 #4）：`AUTOPILOT_EVAL_MEMORY=0` 使 done corpus 為空 → 相似層全放行，
   與舊精確比對關閉行為逐位等價。

驗收標準 #2 範圍界定（第 2 輪 critic 退回後校正，與需求對齊）：相似層攔的是「共享核心詞的改寫」；
驗收標準原點名的「強化提案去重」對「改善去重邏輯」屬**無共享詞的同義改寫**（僅共享「去重」），詞集
Jaccard 遠低於門檻，**不在本相似層範圍**，由第二道子系統廣度防線兜底——以 `test_..._known_limitation`
誠實釘住此邊界，避免無聲行為漂移。

全程走離線假專家（OFFLINE_MODE=True → items 直接取自 OFFLINE_DISCOVERY），不打外部 API。
"""

from __future__ import annotations

import pytest

from studio import autopilot, backlog, config, projects
from studio.improver import ProjectImprover

# corpus 內已 done 的標題（詞集夠豐富，容得下非退化的換詞改寫測試）：
_DONE_TITLE = "改善提案去重的相似度比對邏輯"
# 黑樣本（代表性改寫）：「改善」→「調整」，換詞而非搬字序、非同義詞收斂。
#   Jaccard≈0.786（∈ [0.75, 1.0) 非退化）→ 精確比對擋不到、相似度擋得到。
_BLACK_REWORD = "調整提案去重的相似度比對邏輯"
# 白樣本：語意無關、零詞集交集 → 必須放行、不誤殺。
_WHITE_UNRELATED = "新增登入頁面深色模式"


# --- 第一部分：helper 單一來源契約（不經 _discover，直接打判定函式）-----------------


def test_helper_blocks_reworded_rewrite_non_degenerate():
    """換詞改寫（改善→調整）共享核心詞、Jaccard∈[0.75,1.0) → helper 回命中標題（非 None）。

    這不是語序重排的退化案例：兩標題詞集不完全相同（相似度 < 1.0），仍被攔下，
    證明相似層抓的是真正換句話說的改寫。
    """
    sim = autopilot._token_set_similarity(_BLACK_REWORD, _DONE_TITLE)
    assert config.AUTOPILOT_DEDUP_RATIO <= sim < 1.0  # 非退化：門檻以上、但非完全相同
    assert autopilot._first_similar_title(_BLACK_REWORD, [_DONE_TITLE]) == _DONE_TITLE


def test_helper_passes_unrelated_title():
    """語意無關 → helper 回 None（放行）。"""
    assert autopilot._first_similar_title(_WHITE_UNRELATED, [_DONE_TITLE]) is None


def test_helper_empty_corpus_returns_none():
    """corpus 為空（EVAL_MEMORY=0 的等價情境）→ 一律 None，退回舊關閉行為。"""
    assert autopilot._first_similar_title(_BLACK_REWORD, []) is None


def test_helper_known_limitation_no_shared_word_rewrite():
    """邊界界定（非 bug）：驗收標準原點名的『強化提案去重』與『改善去重邏輯』僅共享『去重』，
    其餘字無交集 → Jaccard < 門檻 → 不在相似層範圍（helper 回 None），由第二道子系統廣度防線兜底。

    這是架構定案（刻意不引 embedding／jieba）的已知取捨。若日後升級策略要攔此類，改動需先更新
    本斷言，避免無聲行為漂移。
    """
    assert autopilot._first_similar_title("強化提案去重", ["改善去重邏輯"]) is None


def test_helper_shares_single_source_with_pending():
    """單一來源證明：pending 防線 `_filter_pending_duplicates` 對同一黑樣本亦丟棄，
    與 done 防線判定一致（兩處走同一個 `_first_similar_title`／`_token_set_similarity`）。"""
    kept = autopilot._filter_pending_duplicates([_BLACK_REWORD, _WHITE_UNRELATED], [_DONE_TITLE])
    assert _BLACK_REWORD not in kept  # pending 相似層擋下換詞改寫
    assert _WHITE_UNRELATED in kept  # 語意無關保留


# --- 第二部分：_discover 端到端（含 dropped 回報，驗收 #6）------------------------


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PROJECTS_ROOT", tmp_path / "projects")
    monkeypatch.setattr(config, "WORKSPACE_ROOT", tmp_path / "ws")
    monkeypatch.setattr(config, "OFFLINE_MODE", True)
    monkeypatch.setattr("studio.improver.OFFLINE_DISCOVERY", [_BLACK_REWORD, _WHITE_UNRELATED])


def _make_improver():
    """建 project、種一筆 done 標題當 corpus，並回傳可攔截 broadcast 事件的 improver。"""
    project = projects.create("去重守門產品", vision="v")
    sdir = projects.state_dir(project["id"])
    task = backlog.add(_DONE_TITLE, source="seed", state_dir=sdir)
    backlog.set_status(task["id"], "done", state_dir=sdir)

    events_seen: list = []

    async def bc(ev):
        events_seen.append(ev.to_dict())

    return ProjectImprover(project, bc), sdir, events_seen


def _dropped_detail(events_seen: list) -> str:
    """取『找問題』階段最後一則 phase_change 的 detail（即含 dropped 摘要的收尾訊息）。"""
    details = [
        e["payload"]["detail"]
        for e in events_seen
        if e["type"] == "phase_change" and e["payload"]["phase"] == "找問題"
    ]
    return details[-1] if details else ""


@pytest.mark.asyncio
async def test_discover_blocks_rewrite_and_reports_dropped(monkeypatch):
    """EVAL_MEMORY>0：換詞改寫被 done 相似層擋下，只入列白樣本；dropped 摘要涵蓋這一擋。"""
    monkeypatch.setattr(config, "AUTOPILOT_EVAL_MEMORY", 5)
    imp, sdir, events_seen = _make_improver()

    n = await imp._discover(sdir)

    pending = [t["title"] for t in backlog.list_tasks("pending", state_dir=sdir)]
    assert _BLACK_REWORD not in pending  # 相似度擋下（精確比對擋不到）
    assert _WHITE_UNRELATED in pending  # 白樣本放行
    assert n == 1
    # 驗收 #6：done 相似層新擋下的 1 個要回報進 dropped 摘要，不靜默丟棄。
    assert "源頭擋掉 1 個" in _dropped_detail(events_seen)


@pytest.mark.asyncio
async def test_discover_exact_reproposal_also_blocked(monkeypatch):
    """精確重提（一字不改）本就相似度=1.0 → 仍被擋；證明升級是超集、不回退既有能力。"""
    monkeypatch.setattr(config, "AUTOPILOT_EVAL_MEMORY", 5)
    monkeypatch.setattr("studio.improver.OFFLINE_DISCOVERY", [_DONE_TITLE, _WHITE_UNRELATED])
    imp, sdir, _ = _make_improver()

    n = await imp._discover(sdir)

    pending = [t["title"] for t in backlog.list_tasks("pending", state_dir=sdir)]
    assert _DONE_TITLE not in pending
    assert _WHITE_UNRELATED in pending
    assert n == 1


@pytest.mark.asyncio
async def test_discover_eval_memory_zero_restores_exact_behavior(monkeypatch):
    """EVAL_MEMORY=0：done corpus 為空 → 相似層全放行，改寫版被保留、無 dropped，向後相容。"""
    monkeypatch.setattr(config, "AUTOPILOT_EVAL_MEMORY", 0)
    imp, sdir, events_seen = _make_improver()

    n = await imp._discover(sdir)

    pending = [t["title"] for t in backlog.list_tasks("pending", state_dir=sdir)]
    assert _BLACK_REWORD in pending  # 開關關閉：改寫版不再被擋
    assert _WHITE_UNRELATED in pending
    assert n == 2
    assert "源頭擋掉" not in _dropped_detail(events_seen)  # 無丟棄
