"""發布通知 email 的 renderer：把 release banner（Markdown）轉成 multipart 兩部件。

由來
====
``release_note.render_email_banner`` 是發布出口的**資料源**，逐字輸出 Markdown
（``## ⚠️ Breaking Changes`` 等標記），刻意保持 import-pure（不碰網路/SMTP，由
``test_release_pipeline_dry_run.py`` 的 import 守門測試強制）。本模組承接
``release_note.py:39`` 的 TODO——「若 email_banner 需要 HTML multipart，將
render_email_banner 拆出獨立 renderer 模組」——把「渲染成 HTML / plain 兩出口」
這層職責拆出來，**不回頭污染** ``release_note.py`` 的離線純度。

職責邊界（與 release_note 一致）：
  - 本模組只**渲染字串**：產出 ``text/plain`` 與 ``text/html`` 兩個 body，外加一個
    純標準庫（``email.message.EmailMessage``）的 multipart/alternative 組裝助手。
  - **不連 SMTP、不送信**：``smtplib`` 與任何網路依賴留在呼叫端，本模組維持離線可跑，
    確保 pre-tag 驗證/CI/無網環境都能渲染與斷言。
  - 四要素（①行為變動 ②原因 ③before/after ④生效版本）與 ``BREAKING_HEADING`` 文字在
    兩出口都須**逐字保留**——HTML 化只改標記、不改文字內容，故 grep 仍抓得到。

Markdown→HTML 範圍說明
======================
``markdown_to_html`` 是**貼合 release banner 結構的務實子集**，非完整 CommonMark：
涵蓋 heading（``#``～``######``）、無序清單（``-``/``*``/``+``）、fenced code
（``` / ~~~，內容原樣保留不做行內處理）、blockquote（``>``）、段落，以及行內
``**bold**``、`` `code` ``、``[text](url)`` 連結。已 HTML-escape 後才套行內規則，故
使用者內容不會破壞標記；fenced code 內**不**做行內處理，避免範例碼被誤格式化。
"""

from __future__ import annotations

import re
from html import escape

from studio.release_note import render_email_banner

# 行首（允許前導空白）連續 3+ 個 ` 或 ~ 視為 code fence 起訖。
_FENCE_RE = re.compile(r"^[ \t]*(`{3,}|~{3,})\s*([^`~]*)$")
# heading：行首 1~6 個 #，後接至少一個空白與標題文字。
_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.*)$")
# 無序清單項：行首（允許前導空白）-、* 或 + 後接空白。
_BULLET_RE = re.compile(r"^[ \t]*[-*+][ \t]+(.*)$")
# blockquote：行首 > （後可緊接空白或內容）。
_QUOTE_RE = re.compile(r"^[ \t]*>[ \t]?(.*)$")

# 行內標記（套在已 escape 的文字上）。
_BOLD_RE = re.compile(r"\*\*([^*]+)\*\*")
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_INLINE_CODE_RE = re.compile(r"`([^`]+)`")
# code span 暫存哨兵：文字已 HTML-escape，\x00 不會出現，可安全當佔位符邊界。
_STASH_RE = re.compile("\x00(\\d+)\x00")


def _render_inline(escaped: str) -> str:
    """把已 HTML-escape 的文字套上行內標記（code/bold/link）。

    先把行內 ``code`` span 抽出存放、換成哨兵佔位符，再套 bold/link，最後還原 code——
    如此 ``code`` 內的 ``**``/``[]()`` 不被誤格式化（與 Markdown 語意一致），同時**容許
    bold 跨越 code span**（如 ``**已改為 `strict` 預設**``），不會漏掉外層粗體。
    """
    codes: list[str] = []

    def _stash(m: re.Match[str]) -> str:
        codes.append(m.group(1))
        return f"\x00{len(codes) - 1}\x00"

    text = _INLINE_CODE_RE.sub(_stash, escaped)
    text = _BOLD_RE.sub(r"<strong>\1</strong>", text)
    text = _LINK_RE.sub(r'<a href="\2">\1</a>', text)
    return _STASH_RE.sub(lambda m: f"<code>{codes[int(m.group(1))]}</code>", text)


def markdown_to_html(md: str) -> str:
    """把 release banner 的 Markdown 子集轉為 HTML 片段（不含 <html>/<body> 外殼）。

    回傳值適合直接塞進 multipart email 的 ``text/html`` 部件。完整支援範圍見模組 docstring。
    """
    lines = md.splitlines()
    html: list[str] = []

    in_fence = False
    fence_char = ""
    code_buf: list[str] = []

    para_buf: list[str] = []
    quote_buf: list[str] = []
    in_list = False

    def flush_para() -> None:
        if para_buf:
            html.append(f"<p>{_render_inline(escape(' '.join(para_buf)))}</p>")
            para_buf.clear()

    def flush_quote() -> None:
        if quote_buf:
            inner = _render_inline(escape(" ".join(quote_buf)))
            html.append(f"<blockquote><p>{inner}</p></blockquote>")
            quote_buf.clear()

    def close_list() -> None:
        nonlocal in_list
        if in_list:
            html.append("</ul>")
            in_list = False

    def flush_blocks() -> None:
        flush_para()
        flush_quote()
        close_list()

    for line in lines:
        fence_m = _FENCE_RE.match(line)
        if fence_m:
            marker = fence_m.group(1)[0]
            if not in_fence:
                # 開 fence：先收掉前面累積的塊級內容。
                flush_blocks()
                in_fence, fence_char = True, marker
                code_buf = []
            elif marker == fence_char:
                in_fence = False
                body = escape("\n".join(code_buf))
                html.append(f"<pre><code>{body}</code></pre>")
            else:
                code_buf.append(line)
            continue
        if in_fence:
            code_buf.append(line)
            continue

        if not line.strip():
            flush_blocks()
            continue

        heading_m = _HEADING_RE.match(line)
        if heading_m:
            flush_blocks()
            level = len(heading_m.group(1))
            text = _render_inline(escape(heading_m.group(2).strip()))
            html.append(f"<h{level}>{text}</h{level}>")
            continue

        bullet_m = _BULLET_RE.match(line)
        if bullet_m:
            flush_para()
            flush_quote()
            if not in_list:
                html.append("<ul>")
                in_list = True
            html.append(f"<li>{_render_inline(escape(bullet_m.group(1).strip()))}</li>")
            continue

        quote_m = _QUOTE_RE.match(line)
        if quote_m:
            flush_para()
            close_list()
            quote_buf.append(quote_m.group(1).strip())
            continue

        # 一般段落行；清單/引用未收尾時先收掉，避免縮排接續行黏進去。
        if in_list or quote_buf:
            flush_blocks()
        para_buf.append(line.strip())

    # 收尾：fence 未關時仍盡力吐出已收的內容，不丟資料。
    if in_fence and code_buf:
        html.append(f"<pre><code>{escape(chr(10).join(code_buf))}</code></pre>")
    flush_blocks()
    return "\n".join(html)


def markdown_to_plain(md: str) -> str:
    """把 release banner 的 Markdown 子集降為可讀純文字（給 ``text/plain`` 部件）。

    去掉純標記字元（``#`` heading 前綴、``**`` 粗體、行內 `` ` ``、fence 圍欄），
    連結 ``[t](u)`` 還原成 ``t (u)``，保留清單 ``- ``、引用文字與四要素文字本身。
    """
    out: list[str] = []
    in_fence = False
    fence_char = ""
    for line in md.splitlines():
        fence_m = _FENCE_RE.match(line)
        if fence_m:
            marker = fence_m.group(1)[0]
            if not in_fence:
                in_fence, fence_char = True, marker
            elif marker == fence_char:
                in_fence = False
            continue  # 圍欄行本身不輸出
        if in_fence:
            out.append(line)
            continue

        text = line
        heading_m = _HEADING_RE.match(text)
        if heading_m:
            text = heading_m.group(2)
        quote_m = _QUOTE_RE.match(text)
        if quote_m and not _BULLET_RE.match(text):
            text = quote_m.group(1)
        text = _LINK_RE.sub(r"\1 (\2)", text)
        text = _BOLD_RE.sub(r"\1", text)
        text = text.replace("`", "")
        out.append(text.rstrip())
    return "\n".join(out).strip()


def render_email_banner_html(changelog_text: str, version: str) -> str:
    """渲染發布通知 email 的 ``text/html`` body。

    以 ``release_note.render_email_banner`` 為單一資料源（保住四要素與 heading 不漂移），
    再經 ``markdown_to_html`` 轉成 HTML。缺 Breaking 區塊時由上游拋 ``MissingBreakingBlock``。
    """
    return markdown_to_html(render_email_banner(changelog_text, version))


def render_email_banner_plain(changelog_text: str, version: str) -> str:
    """渲染發布通知 email 的 ``text/plain`` body（Markdown 標記降為純文字）。"""
    return markdown_to_plain(render_email_banner(changelog_text, version))


def render_email_banner_alternatives(changelog_text: str, version: str) -> dict[str, str]:
    """一次渲染兩出口部件，回傳 ``{"plain": ..., "html": ...}``。

    兩部件同源於 ``render_email_banner``，內容一致、四要素皆不遺失；供呼叫端組
    multipart/alternative。需要現成的 ``EmailMessage`` 物件時用
    ``build_email_banner_message``。
    """
    return {
        "plain": render_email_banner_plain(changelog_text, version),
        "html": render_email_banner_html(changelog_text, version),
    }


def build_email_banner_message(
    changelog_text: str,
    version: str,
    *,
    subject: str | None = None,
    from_addr: str | None = None,
    to_addrs: str | list[str] | None = None,
):
    """組一封 multipart/alternative 的 :class:`email.message.EmailMessage`（plain + html）。

    純標準庫、**不送信**：回傳已設好兩部件與選填表頭的 message 物件，呼叫端自行交給
    ``smtplib``（網路依賴留在呼叫端，本模組維持離線可跑）。``subject`` 省略時以版本帶出
    預設主旨。``email.message`` 為標準庫渲染層，與 ``smtplib`` 無關，不破壞離線性。
    """
    from email.message import EmailMessage  # 區域 import：純標準庫渲染，無網路依賴

    parts = render_email_banner_alternatives(changelog_text, version)
    msg = EmailMessage()
    if subject is None:
        subject = f"Ti Studio {version} 發布通知 — 含破壞性變更，請先閱讀"
    msg["Subject"] = subject
    if from_addr:
        msg["From"] = from_addr
    if to_addrs:
        msg["To"] = ", ".join(to_addrs) if isinstance(to_addrs, list) else to_addrs
    # set_content + add_alternative 會自動成 multipart/alternative：plain 先、html 後
    # （RFC 2046 規定「最豐富的表示放最後」，client 優先挑 html）。
    msg.set_content(parts["plain"])
    msg.add_alternative(parts["html"], subtype="html")
    return msg
