"""守住 `_clip` 不可破壞下游 marker 解析。

Commit message 建議:
test: prove clip marker equivalence catches dropped marker tails
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import pytest

from studio import flow
from studio.discussion import (
    PREV_SEGMENT_MAX_CHARS,
    DiscussionEngine,
    Mention,
    parse_mentions,
)

Parser = Callable[[str], object]
Clipper = Callable[[str, int], str]

PARTICIPANTS = ("工程師", "架構師", "測試員")


@dataclass(frozen=True)
class MarkerCase:
    name: str
    parser: Parser
    long_text: str
    short_text: str


def _long_tail_sample(marker_tail: str) -> str:
    return (
        "前段只是舊討論，不能影響最新結論。\n"
        + ("歷史內容\n" * (PREV_SEGMENT_MAX_CHARS // 5))
        + "\n"
        + marker_tail
    )


def _mentions(text: str) -> list[Mention]:
    return parse_mentions("工程師", text, PARTICIPANTS)


CASES = (
    MarkerCase(
        name="qa_passed",
        parser=flow.qa_passed,
        long_text=_long_tail_sample("實測失敗，需要修正。\n驗證: FAIL\n"),
        short_text="單檔測試通過。\n驗證: PASS",
    ),
    MarkerCase(
        name="senior_approved",
        parser=flow.senior_approved,
        long_text=_long_tail_sample("仍有阻斷問題。\n決議: 退回\n"),
        short_text="風險可接受。\n決議: 核可",
    ),
    MarkerCase(
        name="parse_core_changes",
        parser=flow.parse_core_changes,
        long_text=_long_tail_sample("核心改動: [P0/bug] 修正 discussion 裁剪保留策略\n"),
        short_text="核心改動: [P2/improvement] 補上壓縮守護測試",
    ),
    MarkerCase(
        name="parse_mentions",
        parser=_mentions,
        long_text=_long_tail_sample(
            "回應 @架構師: 同意 方案夠簡單\n回應 @測試員：反對 需要補黑樣本\n"
        ),
        short_text="回應 @架構師: 同意 先用最小可行測試",
    ),
)


def _assert_equivalent_after_clip(parser: Parser, text: str, clipper: Clipper) -> None:
    assert parser(clipper(text, PREV_SEGMENT_MAX_CHARS)) == parser(text)


def _broken_drop_marker_tail(text: str, cap: int) -> str:
    text = (text or "").strip()
    if len(text) <= cap:
        return text
    return "…（後段截斷）" + text[:cap]


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
def test_clip_preserves_long_tail_marker_equivalence(case: MarkerCase) -> None:
    assert len(case.long_text) > PREV_SEGMENT_MAX_CHARS

    _assert_equivalent_after_clip(case.parser, case.long_text, DiscussionEngine._clip)


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
def test_clip_short_marker_samples_are_noop_equivalent(case: MarkerCase) -> None:
    assert len(case.short_text) <= PREV_SEGMENT_MAX_CHARS
    assert DiscussionEngine._clip(case.short_text, PREV_SEGMENT_MAX_CHARS) == case.short_text

    _assert_equivalent_after_clip(case.parser, case.short_text, DiscussionEngine._clip)


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
def test_broken_clipper_that_drops_marker_tail_is_caught(case: MarkerCase) -> None:
    """自證：破壞版壓縮器只保留頭段，吃掉尾段裁決/marker 行；同組等價檢查必拋 AssertionError。"""
    with pytest.raises(AssertionError):
        _assert_equivalent_after_clip(case.parser, case.long_text, _broken_drop_marker_tail)
