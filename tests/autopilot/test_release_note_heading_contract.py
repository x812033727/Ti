"""任務 #2：鎖死 Breaking Changes heading 字串契約。

對應驗收標準 #4：期望 heading 字串 `## ⚠️ Breaking Changes` 在程式碼中寫死為契約
（`studio.release_note.BREAKING_HEADING` 為唯一事實來源），測試引用同一常數。

設計（依架構決策）：
  - 契約以 `BREAKING_HEADING` 常數承載，不另立 CONTRIBUTING.md prose（會漂移）。
  - 「測試 import 此常數」本身即 CI 強制機制：常數改名／模組搬路徑 → import 爆炸。
  - 逐字斷言常數值＋逐字斷言 CHANGELOG 仍含此 heading 行，任何漂移翻紅。
  - 反向黑樣本：把 heading 改成 `## Breaking`（或拿掉 emoji）後，比對必須翻紅，
    證明真鑑別力而非「字串存在」的假綠。
"""

from __future__ import annotations

import re
from pathlib import Path

from studio.release_note import BREAKING_HEADING

ROOT = Path(__file__).resolve().parents[2]
CHANGELOG = ROOT / "CHANGELOG.md"

# 期望的字面契約值（emoji 為契約一部分，不可省略）。
# 例外說明：release_note.py docstring 規定「不得在他處再寫一份字面值」，此 golden
# value 是**唯一允許的例外**——沒有一份獨立字面值就無法驗證 BREAKING_HEADING 常數
# 本身不漂移（常數與斷言值都被一起改錯時須有外部基準擋住）。除此處外不得再複製。
EXPECTED_HEADING = "## ⚠️ Breaking Changes"


def _heading_line_present(text: str, heading: str) -> bool:
    """heading 是否以「獨立一行的頂層 `## ` 區塊」形式存在於文字中。

    用 multiline anchor 逐行比對整行（rstrip 容忍行尾空白），避免被內文裡的
    `[⚠️ Breaking Changes](#...)` 連結或 `### ⚠️ Breaking Changes` 版本節誤判。
    """
    pat = r"(?m)^" + re.escape(heading) + r"\s*$"
    return re.search(pat, text) is not None


def test_constant_is_exact_contract_value():
    """常數值逐字鎖死；若有人改成 `## Breaking` 或拿掉 emoji，此處先翻紅。"""
    assert (
        BREAKING_HEADING == EXPECTED_HEADING
    ), f"BREAKING_HEADING 契約被改動：{BREAKING_HEADING!r} != {EXPECTED_HEADING!r}"


def test_changelog_contains_contract_heading():
    """CHANGELOG.md 必須含逐字相符的頂層 heading 行（引用同一常數，非另寫字面值）。"""
    text = CHANGELOG.read_text(encoding="utf-8")
    assert _heading_line_present(text, BREAKING_HEADING), (
        f"CHANGELOG.md 缺頂層 heading 行 {BREAKING_HEADING!r}"
        f"（有人可能改成 `## Breaking` 或拿掉 emoji，致抽取/比對漏抓）"
    )


# --- 反向黑樣本：證明真鑑別力 ---


def test_black_sample_heading_renamed_to_breaking():
    """把 heading 改成 `## Breaking` 後，逐行比對必須翻紅。"""
    text = CHANGELOG.read_text(encoding="utf-8")
    # 前置斷言：確保替換確實命中。否則 CHANGELOG 已漂移時 re.sub 無作用，
    # polluted == text，下方 assert not 仍無條件通過＝孤立執行假綠。
    assert _heading_line_present(
        text, BREAKING_HEADING
    ), "前置條件失效：替換前 CHANGELOG 必須含 heading（否則黑樣本驗不到鑑別力）"
    polluted = re.sub(r"(?m)^" + re.escape(BREAKING_HEADING) + r"\s*$", "## Breaking", text)
    assert not _heading_line_present(
        polluted, BREAKING_HEADING
    ), "黑樣本失效：heading 改名為 `## Breaking` 後仍被判為存在"


def test_black_sample_emoji_stripped():
    """拿掉 emoji（改成 `## Breaking Changes`）後，逐行比對必須翻紅。"""
    text = CHANGELOG.read_text(encoding="utf-8")
    # 前置斷言：同上，防止 CHANGELOG 已漂移時黑樣本孤立執行假綠。
    assert _heading_line_present(
        text, BREAKING_HEADING
    ), "前置條件失效：替換前 CHANGELOG 必須含 heading（否則黑樣本驗不到鑑別力）"
    polluted = re.sub(r"(?m)^" + re.escape(BREAKING_HEADING) + r"\s*$", "## Breaking Changes", text)
    assert not _heading_line_present(
        polluted, BREAKING_HEADING
    ), "黑樣本失效：拿掉 emoji 後仍被判為存在"
