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
from studio.release_note import BREAKING_HEADING, pyproject_version

# 四要素偵測規則與兩出口清單抽到共用模組（單一事實來源），與 task-3 共用同一份——
# 避免兩檔各自定義 FOUR_ELEMENTS 靜默漂移，互相的鑑別力標準才不分歧。
from tests.autopilot._release_check import (
    FOUR_ELEMENTS,
    OUTLETS,
    has_heading,
    missing_elements,
    outlet_carries_block,
    render_or_none,
    version_matches_effective,
)

CHANGELOG_PATH = Path(__file__).resolve().parents[2] / "CHANGELOG.md"


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
    assert has_heading(body), f"AC#3：{outlet_name} 缺 heading {BREAKING_HEADING!r}"
    missing = missing_elements(body)
    assert not missing, f"AC#3：{outlet_name} 缺四要素 {missing}"


def test_pretag_effective_version_matches_pyproject_in_both_outlets(changelog, version):
    """AC#2：④ 生效版本須逐字對應 pyproject 版本，而非只在 body 任處出現。"""
    assert re.fullmatch(r"\d+\.\d+\.\d+", version), f"pyproject 版本格式異常：{version!r}"
    for name, renderer in OUTLETS:
        body = renderer(changelog, version)
        assert version in body, f"AC#2：{name} 出口未帶 pyproject 版本字串 {version!r}"
        assert version_matches_effective(
            body, version
        ), f"AC#2：{name} 的 ④ 生效版本未對應 pyproject 版本 {version!r}"


# ---------------------------------------------------------------------------
# 黑樣本（AC #5）：成對自證——先證 baseline 綠，再證 mutation 後紅
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("outlet_name,renderer", OUTLETS)
def test_black_sample_missing_block_pairs_red(outlet_name, renderer, changelog, version):
    """抽掉 Breaking heading → 出口翻紅；先自證 baseline 本來是綠的（排除孤立假綠）。"""
    # 正向基線：原始 CHANGELOG 此出口本來完整帶出區塊。
    baseline = renderer(changelog, version)
    assert outlet_carries_block(
        baseline
    ), f"基線失效：{outlet_name} 對原始 CHANGELOG 本應帶出完整區塊，黑樣本無從證偽"
    # mutation：把契約 heading 抽掉。
    polluted = re.sub(r"(?m)^" + re.escape(BREAKING_HEADING) + r"\s*$", "## Notes", changelog)
    assert polluted != changelog, "mutation 為空操作：heading 未被改動，黑樣本無效"

    body = render_or_none(renderer, polluted, version)
    assert body is None or not outlet_carries_block(
        body
    ), f"黑樣本失效：{outlet_name} 缺區塊仍被判為完整帶出（假綠）"


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
    assert name not in missing_elements(
        baseline
    ), f"基線失效：{outlet_name} 原始 body 本應帶到要素「{name}」"

    # mutation：同時抹除圈號錨與語意關鍵字，確保兩錨點都不再命中。
    polluted = re.sub(anchor, "x", changelog)
    polluted = re.sub(semantic, "x", polluted, flags=re.IGNORECASE)
    assert polluted != changelog, f"mutation 為空操作：要素「{name}」未被改動，黑樣本無效"

    body = render_or_none(renderer, polluted, version)
    assert body is None or name in missing_elements(
        body
    ), f"黑樣本失效：{outlet_name} 移除要素「{name}」後仍被判為帶到（假綠）"


@pytest.mark.parametrize("outlet_name,renderer", OUTLETS)
def test_black_sample_stale_effective_version_pairs_red(outlet_name, renderer, changelog, version):
    """只把 ④ 生效版本改成舊版 → 兩出口版本對應斷言必翻紅。"""
    old_version = "0.1.9" if version != "0.1.9" else "0.1.8"

    baseline = renderer(changelog, version)
    assert version_matches_effective(
        baseline, version
    ), f"基線失效：{outlet_name} 原始 body 的 ④ 生效版本本應對應 {version!r}"

    polluted = re.sub(
        r"(?m)(^.*④\s*生效版本[^\n]*自\s*`?)" + re.escape(version) + r"(`?\s*起[^\n]*$)",
        rf"\g<1>{old_version}\2",
        changelog,
        count=1,
    )
    assert polluted != changelog, "mutation 為空操作：④ 生效版本行未被改動，黑樣本無效"

    body = renderer(polluted, version)
    assert version in body, "黑樣本前提失效：外層 heading/footer 應仍帶 pyproject 版本"
    assert not version_matches_effective(
        body, version
    ), f"黑樣本失效：{outlet_name} 的 ④ 生效版本改成 {old_version!r} 仍被判為對應"
