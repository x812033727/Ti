"""Release 兩出口檢測工具——tag notes / email banner 驗證的**單一事實來源**。

task-3（`test_release_pipeline_dry_run.py`）與 task-4
（`test_qa_task4_pretag_breaking_outlets.py`）皆 import 本模組，避免「四要素偵測規則」
散成多份各自漂移：若有人增修 semantic regex，兩個測試檔同步生效，鑑別力標準不分歧。

非測試模組（檔名以底線開頭、非 `test_` 前綴），pytest 不會當測試蒐集。
"""

from __future__ import annotations

import re

from studio.release_note import (
    BREAKING_HEADING,
    MissingBreakingBlock,
    render_email_banner,
    render_tag_notes,
)

# 兩出口 (名稱, renderer)；所有「兩出口皆須」斷言對此迭代，杜絕漏測其一（AC#3）。
OUTLETS = (
    ("tag_notes", render_tag_notes),
    ("email_banner", render_email_banner),
)

# 四要素偵測錨點。貼合 AC#3 字面「①行為變動 ②原因 ③before/after ④生效版本」，
# 以 CHANGELOG 明確標註的圈號 marker 為主錨；輔以語意關鍵字交叉確認（圈號在但
# 語意被抽換時亦能翻紅）。每個 tuple = (要素名, 圈號錨 regex, 語意關鍵字 regex)。
FOUR_ELEMENTS = (
    ("行為變動", r"①\s*行為變動", r"strict[^\n]{0,30}預設|已改為[^\n]{0,20}strict"),
    ("原因", r"②\s*原因", r"symlink|root-?only|root\s*-?\s*only"),
    ("before/after", r"③\s*before\s*/\s*after", r"之前.{0,40}之後|before\s*/\s*after"),
    ("生效版本", r"④\s*生效版本", r"自\s*`?\d+\.\d+\.\d+`?\s*起|生效版本"),
)


def has_heading(body: str) -> bool:
    """出口 body 是否含逐行獨立的 Breaking Changes heading（引用同一常數）。"""
    return re.search(r"(?m)^" + re.escape(BREAKING_HEADING) + r"\s*$", body) is not None


def missing_elements(body: str) -> list[str]:
    """回傳出口 body 缺漏的要素名清單；四要素齊備時回空 list。

    每個要素須**圈號錨與語意關鍵字皆命中**——任一缺即視為未帶到，
    確保黑樣本抽掉圈號或抽掉語意內容任一者都能翻紅。
    """
    missing = []
    for name, anchor, semantic in FOUR_ELEMENTS:
        if not (re.search(anchor, body) and re.search(semantic, body, re.IGNORECASE)):
            missing.append(name)
    return missing


def version_matches_effective(body: str, version: str) -> bool:
    """④ 生效版本是否逐字對應 pyproject 版本。

    補 `FOUR_ELEMENTS[3]` 只驗「有生效版本語意」但不敏感於版本值的缺口；
    必須錨定 ④ 那句，不能用 `version in body`，避免 heading/footer 的版本字串造成假綠。
    """
    pattern = r"(?m)^.*④\s*生效版本[^\n]*自\s*`?" + re.escape(version) + r"`?\s*起[^\n]*$"
    return re.search(pattern, body) is not None


def outlet_carries_block(body: str) -> bool:
    """單一出口是否完整帶出 Breaking 區塊：heading＋四要素全到（正向與黑樣本同一把尺）。"""
    return has_heading(body) and not missing_elements(body)


def render_or_none(renderer, text: str, version: str) -> str | None:
    """渲染；缺區塊拋 MissingBreakingBlock 時回 None，

    讓黑樣本能同時涵蓋『拋例外』與『內容殘缺』兩種翻紅。
    """
    try:
        return renderer(text, version)
    except MissingBreakingBlock:
        return None
