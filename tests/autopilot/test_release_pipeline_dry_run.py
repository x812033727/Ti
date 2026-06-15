"""QA 任務 #3：驗證 release pipeline 兩出口渲染（tag notes / email banner）。

對應驗收標準：
  #3 tag notes body 與 email banner body **兩個出口**渲染後均可被 grep 到
     `Breaking Changes` heading 及四要素（①行為變動 ②原因 ③before/after ④生效版本）。
  #1 抽取器缺區塊時明確 fail（呼叫端 render 拋 MissingBreakingBlock，不靜默產空出口）。
  #2 版本字串由 pyproject.toml 讀取，渲染不硬寫版本。
  #5 反向黑樣本：缺區塊／缺任一要素時，兩出口驗證必翻紅（真鑑別力，非假綠）。
  #6 驗證離線可跑：dry_run 只走記憶體／檔案，不打 gh release API、不連 SMTP。

設計（破壞性思考）：
  - 用 dry_run_dump 把兩出口 dump 成「實際 body 字串」再 grep 比對——杜絕
    「pipeline 說發了、內容沒帶到」的靜默失敗（只驗 config 不算數）。
  - 同一把尺（`_outlet_carries_block`）給正向與黑樣本共用：正向 assert True、
    黑樣本 assert False，移除任一要素即翻紅，證明非「字串存在」的假綠。
  - heading 引用 `BREAKING_HEADING` 常數，不另寫字面值（常數漂移 → import/比對翻紅）。
  - 版本以 `pyproject_version()` 動態取，斷言「該版本出現在兩出口」，測試不硬寫數字。
  - EOF 邊界 fixture（Breaking 為最後 section，後無 `## `）：架構決策點名的盲區，
    若 extractor 漏掉 `\\Z`/EOF 收尾，此 fixture 會讓兩出口渲染翻紅。
  - 離線性：斷言被測模組原始碼不 import 任何網路/子行程依賴，證明 dry-run 真離線。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

# 直接 import 待測 pipeline 函式與唯一事實來源常數。
# 此 import 本身即 CI 強制契約：函式改名／模組搬路徑 → import 爆炸。
from studio.release_note import (
    BREAKING_HEADING,
    MissingBreakingBlock,
    dry_run_dump,
    pyproject_version,
    render_email_banner,
    render_tag_notes,
)

# 四要素偵測規則與兩出口清單、檢測器抽到共用模組（單一事實來源）——
# task-3／task-4 共用同一份，避免兩檔各自定義 FOUR_ELEMENTS 靜默漂移。
from tests.autopilot._release_check import (
    FOUR_ELEMENTS,
    OUTLETS,
    has_heading as _has_heading,
    missing_elements as _missing_elements,
    outlet_carries_block as _outlet_carries_block,
    render_or_none as _render_or_none,
)

ROOT = Path(__file__).resolve().parents[2]
CHANGELOG = ROOT / "CHANGELOG.md"
MODULE_SRC = ROOT / "studio" / "release_note.py"


@pytest.fixture(scope="module")
def changelog() -> str:
    assert CHANGELOG.exists(), f"前提失效：缺 CHANGELOG.md {CHANGELOG}"
    return CHANGELOG.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def version() -> str:
    return pyproject_version()


# ---------------------------------------------------------------------------
# 正向（AC #3）：兩出口皆 grep 得到 heading＋四要素
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("outlet_name,renderer", OUTLETS)
def test_outlet_contains_heading(outlet_name, renderer, changelog, version):
    body = renderer(changelog, version)
    assert _has_heading(body), (
        f"AC#3：{outlet_name} 出口缺 Breaking Changes heading {BREAKING_HEADING!r}"
    )


@pytest.mark.parametrize("outlet_name,renderer", OUTLETS)
def test_outlet_contains_four_elements(outlet_name, renderer, changelog, version):
    body = renderer(changelog, version)
    missing = _missing_elements(body)
    assert not missing, f"AC#3：{outlet_name} 出口缺四要素 {missing}"


@pytest.mark.parametrize("outlet_name,renderer", OUTLETS)
def test_outlet_carries_full_block(outlet_name, renderer, changelog, version):
    """整合斷言：單一出口同時帶到 heading＋四要素。"""
    body = renderer(changelog, version)
    assert _outlet_carries_block(body), f"AC#3：{outlet_name} 未完整帶出 Breaking 區塊"


def test_dry_run_dump_both_outlets_carry_block(changelog, version):
    """dry_run_dump 回傳的兩出口字串皆完整帶出 Breaking 區塊。"""
    out = dry_run_dump(changelog, version)
    assert set(out) == {"tag_notes", "email_banner"}, f"出口集合不符：{set(out)}"
    for name, body in out.items():
        assert _outlet_carries_block(body), f"AC#3：dry_run 出口 {name} 未完整帶出區塊"


# ---------------------------------------------------------------------------
# AC #2：版本由 pyproject 讀，渲染不硬寫；兩出口含該版本字串
# ---------------------------------------------------------------------------


def test_version_from_pyproject_appears_in_both_outlets(changelog, version):
    """版本字串來自 pyproject（非硬寫），且在兩出口 body 內可見。"""
    assert re.fullmatch(r"\d+\.\d+\.\d+", version), f"pyproject 版本格式異常：{version!r}"
    out = dry_run_dump(changelog, version)
    for name, body in out.items():
        assert version in body, f"AC#2：{name} 出口未帶 pyproject 版本字串 {version!r}"


def test_dry_run_default_version_uses_pyproject(changelog, version):
    """dry_run_dump 省略 version 時，預設讀 pyproject（與顯式傳入結果一致）。"""
    default_out = dry_run_dump(changelog)  # 不傳 version
    explicit_out = dry_run_dump(changelog, version)
    assert default_out == explicit_out, "預設版本未走 pyproject 單一事實來源"


# ---------------------------------------------------------------------------
# AC #3：dry_run 落檔供 CI artifact grep（模擬離線檔案比對）
# ---------------------------------------------------------------------------


def test_dry_run_dump_writes_files_for_grep(changelog, version, tmp_path):
    """dry_run 把兩出口落成檔案，檔內可被 grep 到 heading（CI artifact 路徑）。"""
    out = dry_run_dump(changelog, version, out_dir=tmp_path)
    tag_file = tmp_path / "tag_notes.md"
    email_file = tmp_path / "email_banner.txt"
    assert tag_file.exists() and email_file.exists(), "AC#3：dry_run 未落出兩出口檔"
    for f in (tag_file, email_file):
        text = f.read_text(encoding="utf-8")
        assert _has_heading(text), f"AC#3：落檔 {f.name} 缺 heading"
        assert not _missing_elements(text), f"AC#3：落檔 {f.name} 缺四要素"


# ---------------------------------------------------------------------------
# AC #1：缺／空區塊時 render 端明確 fail（不靜默產空出口）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("outlet_name,renderer", OUTLETS)
def test_render_raises_on_missing_block(outlet_name, renderer, version):
    """缺 Breaking 區塊：渲染必拋 MissingBreakingBlock，不回空字串。"""
    no_block = "# Changelog\n\n## [0.2.0]\n### Changed\n- 無 breaking 區塊\n"
    with pytest.raises(MissingBreakingBlock):
        renderer(no_block, version)


@pytest.mark.parametrize("outlet_name,renderer", OUTLETS)
def test_render_raises_on_empty_block(outlet_name, renderer, version):
    """heading 在、內容空：仍視為缺區塊，渲染必拋（不產出空 heading 出口）。"""
    empty_block = "# Changelog\n" + BREAKING_HEADING + "\n\n   \n## [0.2.0]\n- x\n"
    with pytest.raises(MissingBreakingBlock):
        renderer(empty_block, version)


def test_dry_run_dump_raises_on_missing_block(version):
    """dry_run 整體在缺區塊時亦 fail-closed（兩出口任一無法產出即整體拋）。"""
    with pytest.raises(MissingBreakingBlock):
        dry_run_dump("# Changelog\n## [0.2.0]\n- 無區塊\n", version)


# ---------------------------------------------------------------------------
# AC #5：反向黑樣本——缺區塊／缺任一要素，兩出口驗證必翻紅（真鑑別力）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("outlet_name,renderer", OUTLETS)
def test_black_sample_missing_block_fails_both_outlets(outlet_name, renderer, changelog, version):
    """把整個 Breaking heading 抽掉 → 出口要嘛拋例外、要嘛不帶區塊，總之翻紅。"""
    polluted = re.sub(
        r"(?m)^" + re.escape(BREAKING_HEADING) + r"\s*$", "## Notes", changelog
    )
    body = _render_or_none(renderer, polluted, version)
    assert body is None or not _outlet_carries_block(body), (
        f"黑樣本失效：{outlet_name} 缺區塊仍被判為帶出完整區塊（假綠）"
    )


@pytest.mark.parametrize("outlet_name,renderer", OUTLETS)
@pytest.mark.parametrize("elem_idx", range(len(FOUR_ELEMENTS)))
def test_black_sample_missing_each_element_fails(
    elem_idx, outlet_name, renderer, changelog, version
):
    """逐一抽掉四要素之一（圈號錨＋語意關鍵字皆抹除）→ 該出口驗證必翻紅。

    這是 AC#5 的核心鑑別力證明：移除任一要素，`_outlet_carries_block` 必須由
    True 翻成 False，證明檢測器不是「只要有 heading 就放行」的假綠。
    """
    name, anchor, semantic = FOUR_ELEMENTS[elem_idx]
    # 同時抹除圈號錨與語意關鍵字，確保該要素在兩錨點上都不再命中。
    polluted = re.sub(anchor, "x", changelog)
    polluted = re.sub(semantic, "x", polluted, flags=re.IGNORECASE)

    # 前置斷言：原始 CHANGELOG 此要素本來是命中的，否則 replace 為空操作＝孤立假綠。
    assert not _missing_elements(changelog), "前提失效：原 CHANGELOG 四要素本應齊備"

    body = _render_or_none(renderer, polluted, version)
    assert body is None or name in _missing_elements(body), (
        f"黑樣本失效：{outlet_name} 移除要素「{name}」後仍被判為帶到（假綠）"
    )


def test_black_sample_heading_renamed_fails_outlets(changelog, version):
    """heading 改名為 `## Breaking`（拿掉 emoji）→ 兩出口 heading 比對翻紅。

    模擬有人把契約 heading 改掉導致 pipeline 抽不到正確錨點。
    """
    polluted = re.sub(
        r"(?m)^" + re.escape(BREAKING_HEADING) + r"\s*$", "## Breaking", changelog
    )
    for name, renderer in OUTLETS:
        body = _render_or_none(renderer, polluted, version)
        # 改名後 extractor 抓不到契約 heading → render 拋例外（body=None）。
        assert body is None or not _has_heading(body), (
            f"黑樣本失效：{name} 在 heading 改名後仍判為含契約 heading"
        )


# ---------------------------------------------------------------------------
# EOF 邊界（架構決策點名）：Breaking 為最後 section 時兩出口仍完整
# ---------------------------------------------------------------------------


@pytest.fixture
def changelog_bc_at_eof() -> str:
    """Breaking Changes 為 CHANGELOG 最後一個 section（後無任何 `## `）。"""
    return (
        "# Changelog\n\n"
        + BREAKING_HEADING
        + "\n\n"
        "- **① 行為變動**：已改為 `strict` 預設。\n"
        "- **② 原因**：防止 symlink 攻擊，僅 root-only 可寫。\n"
        "- **③ before / after 遷移範例**：之前未設定即放行；之後須顯式設 warn。\n"
        "- **④ 生效版本**：自 `0.2.0` 起生效。\n"
    )


@pytest.mark.parametrize("outlet_name,renderer", OUTLETS)
def test_eof_boundary_outlets_carry_block(outlet_name, renderer, changelog_bc_at_eof, version):
    """EOF 邊界下兩出口仍完整帶出 heading＋四要素（守 extractor 的 `\\Z`/EOF 收尾）。"""
    body = renderer(changelog_bc_at_eof, version)
    assert _outlet_carries_block(body), (
        f"EOF 邊界缺陷：{outlet_name} 在 Breaking 為末段時未完整帶出區塊"
    )


# ---------------------------------------------------------------------------
# AC #6：離線性——dry-run 路徑不依賴 gh release API / SMTP / 任何網路或子行程
# ---------------------------------------------------------------------------


def test_module_has_no_network_or_subprocess_imports():
    """被測模組原始碼不得 import 網路/子行程依賴，證明 dry-run 真離線可跑。

    破壞性思考：若哪天有人在 render 路徑偷塞 `subprocess.run(['gh', ...])` 或
    `smtplib`，pre-tag 驗證就不再離線，CI/無網環境會炸——此測試把該風險擋在 import 層。
    """
    src = MODULE_SRC.read_text(encoding="utf-8")
    # 只看實際的 import 行，避免 docstring 裡「不打 gh／不連 SMTP」字樣誤命中。
    import_lines = [
        ln for ln in src.splitlines()
        if re.match(r"\s*(import|from)\s+", ln)
    ]
    forbidden = ("smtplib", "subprocess", "socket", "requests", "urllib", "http.client")
    hits = [ln.strip() for ln in import_lines if any(f in ln for f in forbidden)]
    assert not hits, f"AC#6：render 模組引入了網路/子行程依賴，破壞離線性：{hits}"
