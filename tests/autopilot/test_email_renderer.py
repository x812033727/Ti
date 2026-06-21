"""驗證 email_renderer：release banner 的 HTML/plain multipart 兩出口渲染。

對應 ``studio/release_note.py:39`` 的 TODO——把 email_banner 的 HTML multipart 渲染拆成
獨立 renderer 模組。重點驗收：
  - 四要素（①行為變動 ②原因 ③before/after ④生效版本）與 Breaking heading 文字在
    HTML / plain 兩出口都**逐字保留**（HTML 化只改標記、不改文字）。
  - Markdown→HTML 子集正確：heading/清單/fence/blockquote/行內 bold/code/link。
  - 反向黑樣本：缺 Breaking 區塊時兩出口皆拋 MissingBreakingBlock（不靜默產空）。
  - 離線性：模組原始碼不 import 網路/SMTP 依賴。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from studio.email_renderer import (
    build_email_banner_message,
    markdown_to_html,
    markdown_to_plain,
    render_email_banner_alternatives,
    render_email_banner_html,
    render_email_banner_plain,
)
from studio.release_note import BREAKING_HEADING, MissingBreakingBlock, pyproject_version

ROOT = Path(__file__).resolve().parents[2]
CHANGELOG = ROOT / "CHANGELOG.md"
MODULE_SRC = ROOT / "studio" / "email_renderer.py"

# 四要素圈號錨——HTML/plain 化後仍須抓得到（文字逐字保留）。
FOUR_ELEMENT_MARKERS = ["① 行為變動", "② 原因", "③ before / after", "④ 生效版本"]


@pytest.fixture(scope="module")
def changelog() -> str:
    assert CHANGELOG.exists(), f"前提失效：缺 CHANGELOG.md {CHANGELOG}"
    return CHANGELOG.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def version() -> str:
    return pyproject_version()


# --- markdown_to_html 單元 -------------------------------------------------


def test_html_heading_levels():
    assert markdown_to_html("# 大標") == "<h1>大標</h1>"
    assert markdown_to_html("### 小標") == "<h3>小標</h3>"


def test_html_inline_bold_code_link():
    html = markdown_to_html("這是 **粗** 與 `碼` 與 [連結](https://x.test)。")
    assert "<strong>粗</strong>" in html
    assert "<code>碼</code>" in html
    assert '<a href="https://x.test">連結</a>' in html


def test_html_bullets_grouped_in_single_ul():
    html = markdown_to_html("- 甲\n- 乙\n- 丙")
    assert html.count("<ul>") == 1 and html.count("</ul>") == 1
    assert html.count("<li>") == 3


def test_html_fenced_code_is_raw_no_inline():
    html = markdown_to_html("```bash\nexport X=**not_bold**\n```")
    assert "<pre><code>" in html
    # fence 內不做行內處理，** 須原樣保留、不得變 <strong>
    assert "**not_bold**" in html
    assert "<strong>" not in html


def test_html_blockquote():
    html = markdown_to_html("> 引用一行")
    assert "<blockquote><p>引用一行</p></blockquote>" in html


def test_html_escapes_special_chars_outside_code():
    html = markdown_to_html("a < b & c > d")
    assert "&lt;" in html and "&amp;" in html and "&gt;" in html
    assert "<b" not in html.replace("<blockquote", "")  # 沒有意外生成標籤


def test_html_inline_code_not_bolded():
    # code span 內的 ** 不可被當粗體
    html = markdown_to_html("`a ** b`")
    assert "<code>a ** b</code>" in html
    assert "<strong>" not in html


def test_html_bold_can_wrap_inline_code():
    # 外層粗體跨越 code span：須同時生成 <strong> 與 <code>，不得殘留字面 **
    html = markdown_to_html("**已改為 `strict` 預設**")
    assert "<strong>已改為 <code>strict</code> 預設</strong>" in html
    assert "**" not in html


# --- markdown_to_plain 單元 ------------------------------------------------


def test_plain_strips_markers_keeps_text():
    plain = markdown_to_plain("## 標題\n- **粗** 與 `碼`\n[連結](http://x.test)")
    assert "標題" in plain and "#" not in plain
    assert "粗" in plain and "**" not in plain
    assert "碼" in plain and "`" not in plain
    assert "連結 (http://x.test)" in plain


def test_plain_drops_fence_markers_keeps_body():
    plain = markdown_to_plain("```bash\necho hi\n```")
    assert "echo hi" in plain
    assert "```" not in plain


# --- 兩出口整合：四要素與 heading 逐字保留 ---------------------------------


def test_html_outlet_preserves_four_elements(changelog, version):
    html = render_email_banner_html(changelog, version)
    for marker in FOUR_ELEMENT_MARKERS:
        assert marker in html, f"HTML 出口遺失四要素標記：{marker}"


def test_plain_outlet_preserves_four_elements(changelog, version):
    plain = render_email_banner_plain(changelog, version)
    for marker in FOUR_ELEMENT_MARKERS:
        assert marker in plain, f"plain 出口遺失四要素標記：{marker}"


def test_html_outlet_preserves_heading_text_and_version(changelog, version):
    html = render_email_banner_html(changelog, version)
    # heading 文字（去掉 Markdown 的 ## 前綴）與 emoji 須保留
    heading_text = BREAKING_HEADING.lstrip("# ").strip()
    assert heading_text in html, "HTML 出口遺失 Breaking heading 文字"
    assert "⚠️" in html
    assert version in html, "HTML 出口遺失版本字串"


def test_html_outlet_well_formed_no_stray_markdown_heading(changelog, version):
    html = render_email_banner_html(changelog, version)
    # 不應殘留行首的 Markdown heading（已轉成 <hN>）
    assert not re.search(r"(?m)^#{1,6}\s", html)


def test_alternatives_returns_both_parts(changelog, version):
    alts = render_email_banner_alternatives(changelog, version)
    assert set(alts) == {"plain", "html"}
    assert "<" in alts["html"] and ">" in alts["html"]  # html 有標籤
    assert "<h" not in alts["plain"]  # plain 無 HTML 標籤


# --- 反向黑樣本：缺區塊兩出口皆翻紅 ----------------------------------------


@pytest.mark.parametrize(
    "renderer",
    [render_email_banner_html, render_email_banner_plain],
)
def test_missing_block_raises(renderer, version):
    no_block = "# Changelog\n\n## Added\n- 無 breaking 區塊\n"
    with pytest.raises(MissingBreakingBlock):
        renderer(no_block, version)


# --- multipart 組裝（純標準庫、不送信） -----------------------------------


def test_build_message_is_multipart_alternative(changelog, version):
    msg = build_email_banner_message(
        changelog,
        version,
        from_addr="bot@ti.test",
        to_addrs=["a@ti.test", "b@ti.test"],
    )
    assert msg.get_content_type() == "multipart/alternative"
    assert msg["To"] == "a@ti.test, b@ti.test"
    assert version in msg["Subject"]
    payloads = {p.get_content_type() for p in msg.iter_parts()}
    assert payloads == {"text/plain", "text/html"}


def test_build_message_custom_subject(changelog, version):
    msg = build_email_banner_message(changelog, version, subject="自訂主旨")
    assert msg["Subject"] == "自訂主旨"


# --- 離線性守門：本模組原始碼不得 import 網路/SMTP ------------------------


def test_module_has_no_network_imports():
    src = MODULE_SRC.read_text(encoding="utf-8")
    import_lines = [ln for ln in src.splitlines() if re.match(r"\s*(import|from)\s+", ln)]
    forbidden = ("smtplib", "subprocess", "socket", "requests", "urllib", "http.client")
    hits = [ln.strip() for ln in import_lines if any(f in ln for f in forbidden)]
    assert not hits, f"renderer 模組引入網路/SMTP 依賴，破壞離線性：{hits}"
