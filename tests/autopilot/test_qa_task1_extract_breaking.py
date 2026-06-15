"""QA 任務 #1：驗證 `studio.release_note` 的純函式抽取器。

對應驗收標準：
  #1 extractor 為純函式，抽出頂層 Breaking Changes 區塊；缺區塊明確 fail（回 None），
     不靜默回空字串。
  #2 版本字串由 pyproject.toml 讀取（目前 0.2.0），不硬寫。
  #5 反向黑樣本：移除區塊／任一要素後驗證必翻紅（真鑑別力，非假綠）。

破壞性原則：預設東西是壞的——重點放在邊界（EOF、空區塊、邊界終止、heading 變體）
與錯誤路徑，不只測快樂路徑。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from studio.release_note import (
    BREAKING_HEADING,
    extract_breaking_block,
    pyproject_version,
)

ROOT = Path(__file__).resolve().parents[2]
CHANGELOG = ROOT / "CHANGELOG.md"

# 四要素標記（與 CHANGELOG 契約一致）。
FOUR_ELEMENTS = ("① 行為變動", "② 原因", "③ before / after", "④ 生效版本")


@pytest.fixture(scope="module")
def changelog_text() -> str:
    assert CHANGELOG.exists(), f"前提失效：缺 {CHANGELOG}"
    return CHANGELOG.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 正向 / 邊界：抽取契約（#1）
# ---------------------------------------------------------------------------


def test_extract_returns_nonempty_block(changelog_text):
    block = extract_breaking_block(changelog_text)
    assert block is not None, "#1：真實 CHANGELOG 應抽出 Breaking 區塊，竟回 None"
    assert block.strip(), "#1：抽出內容不應為空白"


def test_extract_block_contains_four_elements(changelog_text):
    """抽出內容須含四要素（行為／原因／before-after／生效版本）。"""
    block = extract_breaking_block(changelog_text)
    missing = [e for e in FOUR_ELEMENTS if e not in block]
    assert not missing, f"#1/#5：抽出區塊缺四要素 {missing}"


def test_extract_boundary_stops_at_next_h2(changelog_text):
    """邊界鐵則：區塊終止於下一個頂層 `## `，不得洩漏到版本節。"""
    block = extract_breaking_block(changelog_text)
    assert "[0.2.0]" not in block, "#1：區塊洩漏到 `## [0.2.0]` 版本節（邊界未止）"
    assert "### Changed" not in block, "#1：區塊洩漏到 Changed 子節（邊界未止）"


def test_extract_excludes_heading_line(changelog_text):
    """回傳內容不含 heading 行本身（契約：回 body）。"""
    block = extract_breaking_block(changelog_text)
    assert not block.lstrip().startswith(BREAKING_HEADING), "#1：回傳不應含 heading 行"


def test_extract_at_eof_no_trailing_h2():
    """邊界：Breaking 為最後一個 section（後無任何 `## `），靠 \\Z 仍能抽出。"""
    text = (
        "# Changelog\n\n"
        f"{BREAKING_HEADING}\n"
        "- ① 行為變動：X\n- ② 原因：Y\n- ③ before / after：Z\n- ④ 生效版本：0.2.0\n"
    )
    block = extract_breaking_block(text)
    assert block is not None, "#1：EOF 邊界（後無 ## ）抽取失敗——regex 漏了 \\Z"
    assert "④ 生效版本" in block, "#1：EOF 邊界抽出內容不完整"


def test_extract_block_followed_by_h2_is_bounded():
    """區塊後緊接 `## `：只抽 Breaking 內文，不含後續節。"""
    text = f"{BREAKING_HEADING}\n- keep me\n\n## [0.2.0]\n- DROP me\n"
    block = extract_breaking_block(text)
    assert "keep me" in block, "#1：邊界內內容遺失"
    assert "DROP me" not in block, "#1：邊界外內容被誤抽"


# ---------------------------------------------------------------------------
# 錯誤路徑：缺區塊 / 空區塊 一律回 None（#1，非靜默空字串）
# ---------------------------------------------------------------------------


def test_missing_block_returns_none():
    text = "# Changelog\n\n## [0.2.0]\n### Changed\n- nothing breaking\n"
    assert extract_breaking_block(text) is None, "#1：缺區塊須回 None，不得靜默回空字串"


def test_empty_input_returns_none():
    assert extract_breaking_block("") is None, "#1：空輸入須回 None"


def test_heading_present_empty_body_returns_none():
    """heading 在、底下無內容（常見實務失敗）→ 須回 None，不得回空字串。"""
    text = f"# Changelog\n\n{BREAKING_HEADING}\n\n## [0.2.0]\n- x\n"
    result = extract_breaking_block(text)
    assert result is None, f"#1：空區塊須回 None，竟回 {result!r}（靜默空字串為假綠）"


def test_heading_only_whitespace_body_returns_none():
    text = f"{BREAKING_HEADING}\n   \n\t\n## Next\n- x\n"
    assert extract_breaking_block(text) is None, "#1：純空白內容須回 None"


def test_wrong_heading_variant_not_matched():
    """heading 契約鎖死：`## Breaking`（缺 ⚠️）不應被當成 Breaking 區塊。"""
    text = "## Breaking\n- ① 行為變動：x\n\n## [0.2.0]\n- y\n"
    assert extract_breaking_block(text) is None, "#4：非契約 heading 變體竟被匹配——契約未鎖死"


def test_return_type_is_str_or_none(changelog_text):
    block = extract_breaking_block(changelog_text)
    assert isinstance(block, str), "#1：有區塊時回傳型別須為 str"
    assert extract_breaking_block("") is None or isinstance(extract_breaking_block(""), str)


# ---------------------------------------------------------------------------
# 純函式性質：無副作用、可重現
# ---------------------------------------------------------------------------


def test_extract_is_pure_idempotent(changelog_text):
    """同輸入多次呼叫結果一致（純函式，無隱藏狀態）。"""
    a = extract_breaking_block(changelog_text)
    b = extract_breaking_block(changelog_text)
    assert a == b, "#1：純函式同輸入結果不一致"


def test_extract_does_not_mutate_input(changelog_text):
    """str 不可變，驗證呼叫不改變呼叫端持有的字串內容。"""
    snapshot = str(changelog_text)
    extract_breaking_block(changelog_text)
    assert changelog_text == snapshot, "#1：輸入被改動（非純函式）"


# ---------------------------------------------------------------------------
# 版本單一事實來源（#2）：讀 pyproject，不硬寫
# ---------------------------------------------------------------------------


def test_pyproject_version_is_020():
    assert pyproject_version() == "0.2.0", "#2：pyproject 版本非預期 0.2.0"


def test_pyproject_version_reads_from_given_path(tmp_path):
    """傳入不同 pyproject 路徑回傳對應版本——證明真的在讀檔，非硬寫 0.2.0。"""
    fake = tmp_path / "pyproject.toml"
    fake.write_text('[project]\nname = "x"\nversion = "9.9.9"\n', encoding="utf-8")
    assert pyproject_version(fake) == "9.9.9", "#2：版本疑似硬寫——換 pyproject 仍回 0.2.0"


# ---------------------------------------------------------------------------
# 反向黑樣本（#5）：移除區塊／要素後，正向斷言必翻紅，證明真鑑別力
# ---------------------------------------------------------------------------


def test_black_sample_remove_block_makes_extract_none(changelog_text):
    """移除 heading 後 extractor 必回 None（若仍回內容＝假綠）。"""
    polluted = changelog_text.replace(BREAKING_HEADING, "## Removed Section")
    assert extract_breaking_block(polluted) is None, "#5 黑樣本失效：移除契約 heading 後仍抽出區塊"


def test_black_sample_remove_element_fails_four_element_check(changelog_text):
    """從抽出內容移除任一要素後，四要素斷言必翻紅。"""
    block = extract_breaking_block(changelog_text)
    polluted = block.replace("④ 生效版本", "XXXX")
    missing = [e for e in FOUR_ELEMENTS if e not in polluted]
    assert missing, "#5 黑樣本失效：移除『生效版本』要素後仍判四要素齊全（假綠）"


def test_black_sample_version_not_hardwired(tmp_path):
    """若 pyproject_version 硬寫 0.2.0，此黑樣本會翻紅抓出。"""
    fake = tmp_path / "pyproject.toml"
    fake.write_text('[project]\nversion = "1.2.3"\n', encoding="utf-8")
    assert pyproject_version(fake) != "0.2.0", "#5 黑樣本失效：版本硬寫，換檔仍回 0.2.0"
