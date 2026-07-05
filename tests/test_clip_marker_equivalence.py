"""守住 `_clip` 不可破壞下游 marker 解析。

四函式黑白樣本：qa_passed 用尾段 `驗證: FAIL`、senior_approved 用尾段
`決議: 退回`、parse_core_changes 用尾段 `核心改動: [P0/bug] ...`、
parse_mentions 用尾段 `回應 @架構師: 同意` 與 `回應 @測試員: 反對`。

自證結果：黑樣本用 `_broken_head_clip` 模擬只保留頭段、吃掉尾段裁決/marker
行的破壞版壓縮器；同一組 parser 等價檢查在四個函式都必須 raise AssertionError，
避免只測到 no-op 假綠。

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
    SELF_SEGMENT_MAX_CHARS,
    DiscussionEngine,
    parse_mentions,
)

Parser = Callable[[str], object]
Clipper = Callable[[str, int], str]

PARTICIPANTS = ("工程師", "架構師", "測試員")


@dataclass(frozen=True)
class ParserCase:
    name: str
    cap: int
    text: str
    short_text: str
    parse: Parser


def _tail_marker_text(cap: int, tail: str) -> str:
    filler_line = "背景內容：這段只是占位，壓縮時可以移除。\n"
    min_length = max(cap, PREV_SEGMENT_MAX_CHARS)
    filler = filler_line * (min_length // len(filler_line) + 3)
    return f"{filler}\n{tail.strip()}\n尾段錨點: KEEP"


def _parse_engineer_mentions(text: str) -> object:
    return parse_mentions("工程師", text, PARTICIPANTS)


CASES = (
    ParserCase(
        name="flow.qa_passed",
        cap=PREV_SEGMENT_MAX_CHARS,
        text=_tail_marker_text(
            PREV_SEGMENT_MAX_CHARS,
            "實測失敗，需要修正。\n驗證: FAIL",
        ),
        short_text="QA 結論\n驗證: FAIL",
        parse=flow.qa_passed,
    ),
    ParserCase(
        name="flow.senior_approved",
        cap=SELF_SEGMENT_MAX_CHARS,
        text=_tail_marker_text(
            SELF_SEGMENT_MAX_CHARS,
            "仍有阻斷問題。\n決議: 退回",
        ),
        short_text="審查結論\n決議: 退回",
        parse=flow.senior_approved,
    ),
    ParserCase(
        name="flow.parse_core_changes",
        cap=PREV_SEGMENT_MAX_CHARS,
        text=_tail_marker_text(
            PREV_SEGMENT_MAX_CHARS,
            "核心改動: [P0/bug] 修正 discussion 裁剪保留策略",
        ),
        short_text="核心改動: [P2/improvement] 補上壓縮守護測試",
        parse=flow.parse_core_changes,
    ),
    ParserCase(
        name="discussion.parse_mentions",
        cap=SELF_SEGMENT_MAX_CHARS,
        text=_tail_marker_text(
            SELF_SEGMENT_MAX_CHARS,
            "回應 @架構師: 同意 方案夠簡單\n回應 @測試員：反對 需要補黑樣本",
        ),
        short_text="回應 @架構師: 同意 先用最小可行測試",
        parse=_parse_engineer_mentions,
    ),
)


def _assert_parser_equivalent(case: ParserCase, clipper: Clipper) -> None:
    assert case.parse(clipper(case.text, case.cap)) == case.parse(case.text)


def _broken_head_clip(text: str, cap: int) -> str:
    text = (text or "").strip()
    if len(text) <= cap:
        return text
    return text[:cap]


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
def test_clip_keeps_tail_marker_parser_result(case: ParserCase) -> None:
    assert len(case.text) > case.cap
    assert DiscussionEngine._clip(case.text, case.cap).startswith("…（前段截斷）")

    _assert_parser_equivalent(case, DiscussionEngine._clip)


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
def test_clip_noops_under_cap_and_parser_result_stays_same(case: ParserCase) -> None:
    assert len(case.short_text) < case.cap
    clipped = DiscussionEngine._clip(case.short_text, case.cap)

    assert clipped == case.short_text
    assert case.parse(clipped) == case.parse(case.short_text)


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
def test_black_samples_catch_marker_eating_compressor(case: ParserCase) -> None:
    """破壞版壓縮器吃掉尾段裁決/marker 時，四函式等價檢查都會攔下。"""
    with pytest.raises(AssertionError):
        _assert_parser_equivalent(case, _broken_head_clip)
