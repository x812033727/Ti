"""任務 #4 驗收測試：`_build_discovery_prompt` 隨 pending 子系統分佈，動態列出「已過多」清單。

純字串 / monkeypatch，不打 LLM/網路。涵蓋：
- 子系統抽取（`_extract_subsystems`）正向命中 + 架構要求的誤命中黑樣本零誤殺；
- 計數（`_count_subsystem_coverage`）回傳 Counter、跨標題累加；
- 「已過多」段隨分佈出現/不出現（達 K 才列、未達不列、空清單不出現）；
- prompt 在對應情境包含/不包含該段文字（核心驗收）；
- K 門檻來自單一 config 常數、可調。
"""

from __future__ import annotations

import re
from collections import Counter

import pytest

from studio import autopilot, config

_OVER_HEADER = "下列子系統的排隊任務已過多"


# ---------------------------------------------------------------------------
# 子系統抽取：正向命中
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "title,expected",
    [
        ("替 backlog 模組補測試", {"backlog"}),
        ("重構 orchestrator 啟動順序", {"orchestrator"}),
        ("修復 CI 的 merge 流程", {"ci", "merge"}),
        ("改善去重邏輯", {"去重"}),
        ("優化評估流程", {"評估"}),
        ("為 experts 與 providers 補型別", {"experts", "providers"}),
    ],
)
def test_extract_subsystems_hits(title, expected):
    assert autopilot._extract_subsystems(title) == expected


# ---------------------------------------------------------------------------
# 架構要求的誤命中黑樣本：邊界生效，零誤殺
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "title",
    [
        "social media decide emergence",  # ci/merge 不該打到 social/decide/emergence
        "improve user experience overall",  # experts? 不該打到 experience
        "為設定檔加上 schema 驗證",  # 無任何子系統關鍵詞 → 空集合
    ],
)
def test_extract_subsystems_no_false_positive(title):
    # 抽取行為由 #3 owner（test_autopilot_subsystem_filter.py）定義；此處只 smoke 英文 \b 邊界。
    assert autopilot._extract_subsystems(title) == set()


# ---------------------------------------------------------------------------
# 計數：回傳 Counter、跨標題累加
# ---------------------------------------------------------------------------


def test_count_subsystem_coverage_returns_counter():
    titles = ["替 backlog 補測試", "重構 backlog 索引", "修 CI"]
    counts = autopilot._count_subsystem_coverage(titles)
    assert isinstance(counts, Counter)
    assert counts["backlog"] == 2
    assert counts["ci"] == 1
    assert counts["discovery"] == 0  # 未出現者為 0


# ---------------------------------------------------------------------------
# 「已過多」段：隨分佈出現 / 不出現
# ---------------------------------------------------------------------------


def test_oversub_context_lists_over_quota(monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_SUBSYSTEM_MAX", 2)
    titles = ["替 backlog 補測試", "重構 backlog 索引", "為 backlog 加快取", "修 CI"]
    ctx = autopilot._oversubscribed_context(titles)
    assert _OVER_HEADER in ctx
    assert "backlog（已有 3 筆）" in ctx
    assert "ci" not in ctx  # 只 1 筆，未達門檻不列


def test_oversub_context_empty_when_below_k(monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_SUBSYSTEM_MAX", 2)
    titles = ["替 backlog 補測試", "修 CI", "重構 orchestrator"]  # 每個子系統各 1 筆
    assert autopilot._oversubscribed_context(titles) == ""


def test_oversub_context_empty_for_no_titles(monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_SUBSYSTEM_MAX", 2)
    assert autopilot._oversubscribed_context([]) == ""


def test_oversub_context_k_is_single_config_constant(monkeypatch):
    # K 為單一 config 常數、可調：同一批標題，門檻調高即不再列出。
    titles = ["替 backlog 補測試", "重構 backlog 索引"]  # backlog 2 筆
    monkeypatch.setattr(config, "AUTOPILOT_SUBSYSTEM_MAX", 2)
    assert _OVER_HEADER in autopilot._oversubscribed_context(titles)
    monkeypatch.setattr(config, "AUTOPILOT_SUBSYSTEM_MAX", 3)
    assert autopilot._oversubscribed_context(titles) == ""


# ---------------------------------------------------------------------------
# 核心驗收：prompt 文字在對應情境出現 / 不出現
# ---------------------------------------------------------------------------


def test_prompt_includes_oversub_section_when_over_quota(monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_SUBSYSTEM_MAX", 2)
    titles = ["替 backlog 補測試", "重構 backlog 索引", "為 backlog 加快取"]
    # 單一注入點：傳 titles= 即同時驅動 pending-awareness 與 oversubscribed 兩段（同源同快照），
    # 無須 monkeypatch 全域 _pending_titles。
    prompt = autopilot._build_discovery_prompt(outcomes="", titles=titles)
    assert _OVER_HEADER in prompt
    assert "backlog（已有 3 筆）" in prompt


def test_prompt_excludes_oversub_section_when_balanced(monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_SUBSYSTEM_MAX", 2)
    titles = ["替 backlog 補測試", "修 CI", "重構 orchestrator"]  # 各 1 筆，無人超標
    prompt = autopilot._build_discovery_prompt(outcomes="", titles=titles)
    assert _OVER_HEADER not in prompt
    # 健全性：pending-awareness 清單仍在，確認是「過多段」被略過而非整段空白。
    assert "已在排隊" in prompt


def test_prompt_excludes_oversub_section_when_no_pending(monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_SUBSYSTEM_MAX", 2)
    prompt = autopilot._build_discovery_prompt(outcomes="", titles=[])
    assert _OVER_HEADER not in prompt


# ---------------------------------------------------------------------------
# 抽取規則固定套 IGNORECASE（不由呼叫端決定）
# ---------------------------------------------------------------------------


def test_extract_is_case_insensitive():
    assert autopilot._extract_subsystems("修 BACKLOG 的 Bug") == {"backlog"}
    # 編譯期即帶 IGNORECASE flag，呼叫端無從關閉（#3 的編譯後 pattern 清單）。
    assert all(p.flags & re.IGNORECASE for _, p in autopilot._SUBSYSTEM_COMPILED)
