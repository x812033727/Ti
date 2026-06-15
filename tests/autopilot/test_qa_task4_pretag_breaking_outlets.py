"""QA 任務 #4：pre-tag 驗證閘門——tag notes / email banner 兩出口皆帶 Breaking 區塊。

對應驗收標準（任務 #4）：
  #3 兩出口渲染後皆可 grep 到 `Breaking Changes` heading 及四要素
     （①行為變動 ②原因 ③before/after ④生效版本）。
  #5 反向黑樣本：缺區塊／缺任一要素 → 兩出口驗證必翻紅（真鑑別力，非假綠）。
  #6 0.2.0 tag 打出**之前**離線可跑：只走記憶體字串，不打 gh release API / SMTP。

與 `test_release_pipeline_dry_run.py` 的分工（不重複造輪子）：
  - 該檔在**單元層**驗 renderer 回傳值、dry_run 落檔、缺/空區塊拋例外等。
  - 本檔是**pre-tag 閘門層**：把每個黑樣本做成「正向基線 → 黑樣本」的**成對自證**，
    先斷言原始真實 CHANGELOG 此情境本來是綠的，再斷言 mutation 後翻紅。
    這填補既有檔在「缺區塊黑樣本」缺少正向基線的盲區——若 mutation 是空操作
    （regex 沒命中、改錯目標），基線斷言會先抓到，避免黑樣本變成「永遠綠」的孤立假綠
    （NOTES.md：自證對應＋排除假綠）。

破壞性思考：本閘門的價值在於「移除任一要素必翻紅」，故每條黑樣本都先證明
mutation 真的改動了輸入（baseline 綠 → mutated 紅），而非斷言一個本來就紅的東西。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

# import 即契約：函式/常數改名或模組搬路徑 → import 爆炸，CI 強制鎖死。
from studio.release_note import (
    BREAKING_HEADING,
    MissingBreakingBlock,
    pyproject_version,
    render_email_banner,
    render_tag_notes,
)

CHANGELOG_PATH = Path(__file__).resolve().parents[2] / "CHANGELOG.md"

# 兩出口 (名稱, renderer)；所有「兩出口皆須」斷言對此迭代，杜絕漏測其一。
OUTLETS = (
    ("tag_notes", render_tag_notes),
    ("email_banner", render_email_banner),
)

# 四要素偵測錨點：每項須圈號錨＋語意關鍵字皆命中才算帶到，
# 確保黑樣本抽掉圈號或抽掉語意內容任一者都能翻紅。
FOUR_ELEMENTS = (
    ("行為變動", r"①\s*行為變動", r"strict[^\n]{0,30}預設|已改為[^\n]{0,20}strict"),
    ("原因", r"②\s*原因", r"symlink|root-?only|root\s*-?\s*only"),
    ("before/after", r"③\s*before\s*/\s*after", r"之前.{0,40}之後|before\s*/\s*after"),
    ("生效版本", r"④\s*生效版本", r"自\s*`?\d+\.\d+\.\d+`?\s*起|生效版本"),
)


def _has_heading(body: str) -> bool:
    return re.search(r"(?m)^" + re.escape(BREAKING_HEADING) + r"\s*$", body) is not None


def _missing_elements(body: str) -> list[str]:
    missing = []
    for name, anchor, semantic in FOUR_ELEMENTS:
        if not (re.search(anchor, body) and re.search(semantic, body, re.IGNORECASE)):
            missing.append(name)
    return missing


def _outlet_carries_block(body: str) -> bool:
    """單一出口是否完整帶出 Breaking 區塊：heading＋四要素全到（正向與黑樣本同一把尺）。"""
    return _has_heading(body) and not _missing_elements(body)


def _render_or_none(renderer, text: str, version: str) -> str | None:
    """渲染；缺區塊拋例外時回 None，讓黑樣本能同時涵蓋『拋例外』與『內容殘缺』兩種翻紅。"""
    try:
        return renderer(text, version)
    except MissingBreakingBlock:
        return None


@pytest.fixture(scope="module")
def changelog() -> str:
    assert CHANGELOG_PATH.exists(), f"前提失效：缺 CHANGELOG.md {CHANGELOG_PATH}"
    return CHANGELOG_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def version() -> str:
    return pyproject_version()


# ---------------------------------------------------------------------------
# 正向閘門（AC #3）：真實 CHANGELOG → 兩出口皆完整帶出區塊
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("outlet_name,renderer", OUTLETS)
def test_pretag_outlet_carries_block(outlet_name, renderer, changelog, version):
    """pre-tag 閘門：對真實 CHANGELOG 渲染，出口須含 heading＋四要素全到。"""
    body = renderer(changelog, version)
    assert _has_heading(body), f"AC#3：{outlet_name} 缺 heading {BREAKING_HEADING!r}"
    missing = _missing_elements(body)
    assert not missing, f"AC#3：{outlet_name} 缺四要素 {missing}"


def test_pretag_version_from_pyproject_in_both_outlets(changelog, version):
    """AC#2：版本來自 pyproject（非硬寫），且在兩出口 body 內可見。"""
    assert re.fullmatch(r"\d+\.\d+\.\d+", version), f"pyproject 版本格式異常：{version!r}"
    for name, renderer in OUTLETS:
        body = renderer(changelog, version)
        assert version in body, f"AC#2：{name} 出口未帶 pyproject 版本字串 {version!r}"


# ---------------------------------------------------------------------------
# 黑樣本（AC #5）：成對自證——先證 baseline 綠，再證 mutation 後紅
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("outlet_name,renderer", OUTLETS)
def test_black_sample_missing_block_pairs_red(outlet_name, renderer, changelog, version):
    """抽掉 Breaking heading → 出口翻紅；先自證 baseline 本來是綠的（排除孤立假綠）。"""
    # 正向基線：原始 CHANGELOG 此出口本來完整帶出區塊。
    baseline = renderer(changelog, version)
    assert _outlet_carries_block(baseline), (
        f"基線失效：{outlet_name} 對原始 CHANGELOG 本應帶出完整區塊，黑樣本無從證偽"
    )
    # mutation：把契約 heading 抽掉。
    polluted = re.sub(
        r"(?m)^" + re.escape(BREAKING_HEADING) + r"\s*$", "## Notes", changelog
    )
    assert polluted != changelog, "mutation 為空操作：heading 未被改動，黑樣本無效"

    body = _render_or_none(renderer, polluted, version)
    assert body is None or not _outlet_carries_block(body), (
        f"黑樣本失效：{outlet_name} 缺區塊仍被判為完整帶出（假綠）"
    )


@pytest.mark.parametrize("outlet_name,renderer", OUTLETS)
@pytest.mark.parametrize("elem_idx", range(len(FOUR_ELEMENTS)))
def test_black_sample_missing_each_element_pairs_red(
    elem_idx, outlet_name, renderer, changelog, version
):
    """逐一抽掉四要素之一（圈號錨＋語意關鍵字皆抹除）→ 該出口翻紅。

    成對自證：先確認原始 body 帶到此要素（baseline 綠），再確認 mutation 後此要素缺漏。
    """
    name, anchor, semantic = FOUR_ELEMENTS[elem_idx]

    # baseline：原始出口本來帶到此要素。
    baseline = renderer(changelog, version)
    assert name not in _missing_elements(baseline), (
        f"基線失效：{outlet_name} 原始 body 本應帶到要素「{name}」"
    )

    # mutation：同時抹除圈號錨與語意關鍵字，確保兩錨點都不再命中。
    polluted = re.sub(anchor, "x", changelog)
    polluted = re.sub(semantic, "x", polluted, flags=re.IGNORECASE)
    assert polluted != changelog, f"mutation 為空操作：要素「{name}」未被改動，黑樣本無效"

    body = _render_or_none(renderer, polluted, version)
    assert body is None or name in _missing_elements(body), (
        f"黑樣本失效：{outlet_name} 移除要素「{name}」後仍被判為帶到（假綠）"
    )


# ---------------------------------------------------------------------------
# AC #6：本測試本身不依賴 gh release / SMTP / 網路——0.2.0 tag 前離線可跑
# ---------------------------------------------------------------------------


def test_pretag_validation_runs_offline(changelog, version):
    """整條 pre-tag 閘門只走記憶體字串渲染，無任何網路/子行程/外部 tag 依賴。

    破壞性思考：tag 尚未打出時 `gh release view` 會 404；本閘門刻意只比對
    記憶體渲染結果，確保能在 tag 之前、無網環境下作為 CI gate 跑。
    """
    for _name, renderer in OUTLETS:
        body = renderer(changelog, version)
        assert _outlet_carries_block(body)
