"""任務 #3 黑白樣本守門：done-list 相似度去重的契約與端到端行為。

補在 `tests/test_improver_discover_done_dedup.py`（任務 #2 伴隨測試）之外，聚焦三件事：
1. **單一來源契約**：done 與 pending 兩防線共用 `autopilot._first_similar_title`——直接對 helper 打
   黑白樣本，證明「相似度」而非「精確」判別，並誠實釘住 token-Jaccard 對「無共享字同義改寫」的
   已知漏網（驗收標準舉的『強化提案去重』vs『改善去重邏輯』其實 <門檻，本測試以 xfail 式斷言記錄）。
2. **端到端 dropped 回報**（驗收 #6）：done 相似層新擋下的項目要計入 `_discover` 廣播的
   「源頭擋掉 N 個」訊息，不靜默丟棄。
3. **開關向後相容**（驗收 #4）：`AUTOPILOT_EVAL_MEMORY=0` 使 done corpus 為空 → 相似層全放行，
   與舊精確比對關閉行為逐位等價。

全程走離線假專家（OFFLINE_MODE=True → items 直接取自 OFFLINE_DISCOVERY），不打外部 API。
"""

from __future__ import annotations

import pytest

from studio import autopilot, backlog, config, projects
from studio.improver import ProjectImprover

# 對照 corpus 內已 done 的「改善去重邏輯」：
_DONE_TITLE = "改善去重邏輯"
# 黑樣本：僅語序調換、核心詞集完全相同（Jaccard=1.0 ≥ 0.75）→ 精確比對擋不到、相似度擋得到。
_BLACK_REORDER = "去重邏輯改善"
# 白樣本：語意無關、零詞集交集 → 必須放行、不誤殺。
_WHITE_UNRELATED = "新增登入頁面深色模式"


# --- 第一部分：helper 單一來源契約（不經 _discover，直接打判定函式）-----------------


def test_helper_blocks_reordered_rewrite():
    """語序改寫共享全部核心詞 → helper 回傳命中標題（供 log 指出近似哪一筆），非 None。"""
    hit = autopilot._first_similar_title(_BLACK_REORDER, [_DONE_TITLE])
    assert hit == _DONE_TITLE


def test_helper_passes_unrelated_title():
    """語意無關 → helper 回 None（放行）。"""
    assert autopilot._first_similar_title(_WHITE_UNRELATED, [_DONE_TITLE]) is None


def test_helper_empty_corpus_returns_none():
    """corpus 為空（EVAL_MEMORY=0 的等價情境）→ 一律 None，退回舊關閉行為。"""
    assert autopilot._first_similar_title(_BLACK_REORDER, []) is None


def test_helper_known_limitation_no_shared_word_rewrite():
    """誠實釘住 token-Jaccard 已知限制：驗收標準舉的『強化提案去重』與『改善去重邏輯』
    僅共享『去重』，其餘字無交集 → Jaccard < 門檻 → **漏網**（helper 回 None）。

    這不是 bug 而是架構定案（刻意不引 embedding／jieba）的已知取捨；由第二道子系統廣度防線兜底。
    若日後升級策略要攔下此類，改動需先更新本斷言，避免無聲行為漂移。
    """
    assert autopilot._first_similar_title("強化提案去重", [_DONE_TITLE]) is None


def test_helper_shares_single_source_with_pending():
    """單一來源證明：pending 防線 `_filter_pending_duplicates` 對同一黑樣本亦丟棄，
    與 done 防線判定一致（兩處走同一個 `_first_similar_title`／`_token_set_similarity`）。"""
    kept = autopilot._filter_pending_duplicates([_BLACK_REORDER, _WHITE_UNRELATED], [_DONE_TITLE])
    assert _BLACK_REORDER not in kept  # pending 相似層擋下語序改寫
    assert _WHITE_UNRELATED in kept  # 語意無關保留


# --- 第二部分：_discover 端到端（含 dropped 回報，驗收 #6）------------------------


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PROJECTS_ROOT", tmp_path / "projects")
    monkeypatch.setattr(config, "WORKSPACE_ROOT", tmp_path / "ws")
    monkeypatch.setattr(config, "OFFLINE_MODE", True)
    monkeypatch.setattr("studio.improver.OFFLINE_DISCOVERY", [_BLACK_REORDER, _WHITE_UNRELATED])


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
    """EVAL_MEMORY>0：語序改寫被 done 相似層擋下，只入列白樣本；dropped 摘要涵蓋這一擋。"""
    monkeypatch.setattr(config, "AUTOPILOT_EVAL_MEMORY", 5)
    imp, sdir, events_seen = _make_improver()

    n = await imp._discover(sdir)

    pending = [t["title"] for t in backlog.list_tasks("pending", state_dir=sdir)]
    assert _BLACK_REORDER not in pending  # 相似度擋下（精確比對擋不到）
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
    assert _BLACK_REORDER in pending  # 開關關閉：改寫版不再被擋
    assert _WHITE_UNRELATED in pending
    assert n == 2
    assert "源頭擋掉" not in _dropped_detail(events_seen)  # 無丟棄
