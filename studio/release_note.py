"""Release note 抽取器：從 CHANGELOG.md 抽出頂層 Breaking Changes 區塊。

本模組為 release pipeline 兩出口（tag notes body／email banner body）的上游資料源。
版本字串以 `pyproject.toml` 為單一事實來源，本模組不硬寫任何版本號。

Heading 字串契約（唯一事實來源）
================================
頂層 Breaking Changes 區塊的 heading **必須**逐字為 ``BREAKING_HEADING`` 所定義
的字串（``## ⚠️ Breaking Changes``）。pipeline 以此 heading 為錨點抽出區塊並注入
tag notes / email banner 兩個發佈出口。

為什麼鎖死成常數而非散落各處的字面值：
- 任何抽取／比對端一律 ``from studio.release_note import BREAKING_HEADING``，
  不得在他處再寫一份 ``"## ⚠️ Breaking Changes"`` 字面值。**唯一允許的例外**是
  ``test_release_note_heading_contract.py`` 裡的 golden value——沒有一份獨立字面值
  就無法驗證常數本身不漂移；該處已注釋標明此為例外。
- 若有人把 CHANGELOG 的 heading 改成 ``## Breaking`` 或拿掉 emoji，比對會立刻
  漏抓——測試 ``tests/autopilot/test_release_note_heading_contract.py`` 引用本常數
  逐字斷言 CHANGELOG 仍含此 heading，故任何漂移會在 CI 翻紅。
- 「測試 import 這個常數」本身就是 CI 強制機制：常數改名或模組搬路徑，import 爆炸，
  比 prose 文件（會靜默過時）更可靠，因此不另立 CONTRIBUTING.md 條目。

emoji（⚠️, U+26A0 U+FE0F）是契約的一部分，不可省略——抽取端會 ``re.escape``
本常數，故 emoji 與任何特殊字元都被當字面值處理。

抽取器設計契約（依架構決策）：
  - `extract_breaking_block` 為純查詢函式：找到什麼回什麼，回 None 代表「無此區塊或內容為空」。
    判斷「是否必須存在」是呼叫端（pre-tag validator）的責任，不在 extractor 內拋例外——
    避免把「本次 release 無 breaking changes（合法）」與「格式錯誤」混為一談。
  - 空區塊（strip 後為空字串）與缺區塊一律回 None，讓呼叫端只需一個 `if block is None` 分支。
  - 區塊邊界：自 `## ⚠️ Breaking Changes` 起，至下一個頂層 `## ` 止；若為 CHANGELOG 最後一個
    section（後無任何 `## `），以 EOF 為界仍能抽出。
  - **fence 感知**：邊界判斷用逐行掃描並追蹤 code fence（``` / ~~~）狀態；fence 內的
    `## ` 開頭行（如 before/after 範例碼裡的 shell 註解、markdown 語法展示）**不**視為
    section 邊界。純正則 `^## ` 無法感知 fence，會在區塊內 `## ` 處靜默截斷、回傳殘缺非空
    內容（假綠）——本實作以逐行 fence 狀態機消除此盲區。
"""

# TODO: 若 email_banner 需要 HTML multipart，將 render_email_banner 拆出獨立 renderer 模組。

from __future__ import annotations

import re
import tomllib
from pathlib import Path

#: heading 字串的唯一事實來源。測試與渲染皆引用此常數，禁止硬寫字面量。
#: 改動此字串＝改動發佈契約，務必同步 CHANGELOG.md 與相依測試。
BREAKING_HEADING = "## ⚠️ Breaking Changes"

# 精準匹配「整行即 BREAKING_HEADING」（行尾允許尾隨空白），用 MULTILINE 逐行生效。
_HEADING_RE = re.compile(r"(?m)^" + re.escape(BREAKING_HEADING) + r"[ \t]*$")

# code fence 起訖標記：行首（允許前導空白）連續 3+ 個 ` 或 ~。
_FENCE_RE = re.compile(r"^[ \t]*(`{3,}|~{3,})")

# pyproject.toml 位於 repo 根（本檔在 studio/ 下，往上一層）。
_PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


def extract_breaking_block(text: str) -> str | None:
    """從 CHANGELOG 文字抽出頂層 Breaking Changes 區塊內容。

    回傳區塊內文（不含 heading 行，已 strip）；缺區塊或內容為空時回 None。

    邊界以逐行掃描界定：自 heading 之後起，至下一個**非 fence 內**的頂層 `## ` 行止
    （或 EOF）。fence（``` / ~~~）內的 `## ` 行不視為邊界，避免範例碼靜默截斷區塊。
    """
    m = _HEADING_RE.search(text)
    if m is None:
        return None

    # text[m.end():] 形如 "\n<行1>\n<行2>...";splitlines()[0] 為 heading 行尾的空段，跳過。
    rest_lines = text[m.end():].splitlines()[1:]

    body_lines: list[str] = []
    in_fence = False
    fence_char = ""
    for line in rest_lines:
        fence_m = _FENCE_RE.match(line)
        if fence_m:
            marker = fence_m.group(1)[0]  # '`' 或 '~'
            if not in_fence:
                in_fence, fence_char = True, marker
            elif marker == fence_char:
                in_fence = False
            body_lines.append(line)
            continue
        # 僅在 fence 外、行首為頂層 `## ` 時才視為區塊邊界。
        if not in_fence and line.startswith("## "):
            break
        body_lines.append(line)

    body = "\n".join(body_lines).strip()
    return body or None


def pyproject_version(pyproject_path: Path | None = None) -> str:
    """讀 pyproject.toml 的 project.version，作為版本字串單一事實來源。"""
    path = pyproject_path or _PYPROJECT
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    return data["project"]["version"]


class MissingBreakingBlock(ValueError):
    """CHANGELOG 缺（或為空）頂層 Breaking Changes 區塊，兩出口無法產出。

    這是「呼叫端決定『此次發布必須有 breaking 區塊』」的失敗訊號——extractor 本身
    回 None 不算錯，但渲染兩發布出口時若缺區塊即屬發布流程錯誤（依架構決策，責任在呼叫端）。
    """


# tag notes / email banner 兩出口共用的渲染骨架。兩者皆**逐字注入**同一個抽出區塊，
# 確保四要素（①行為變動 ②原因 ③before/after ④生效版本）在兩出口都不被截斷／改寫。
def _render(changelog_text: str, version: str, *, heading: str, footer: str) -> str:
    """共用渲染骨架。兩參數的格式化約定不同，呼叫端勿混淆：

      - ``heading``：**含 ``{version}`` 佔位符的模板字串**，本函式以 ``.format(version=...)``
        求值。傳入已求值的 f-string 是 no-op；含其他未知 key 會 ``KeyError``。
      - ``footer``：**由呼叫端預先格式化好的最終字串**，本函式不再 ``.format()``。
    """
    block = extract_breaking_block(changelog_text)
    if block is None:
        raise MissingBreakingBlock(
            "CHANGELOG 缺 Breaking Changes 區塊或內容為空，無法產出 release 出口"
        )
    return f"{heading.format(version=version)}\n\n{BREAKING_HEADING}\n\n{block}\n\n{footer}"


def render_tag_notes(changelog_text: str, version: str) -> str:
    """渲染 git tag / GitHub release 的 notes body（markdown）。

    注入頂層 Breaking Changes 區塊，version 由呼叫端傳入（來源為 `pyproject_version`，
    不在此硬寫）。缺區塊時拋 `MissingBreakingBlock`。
    """
    return _render(
        changelog_text,
        version,
        heading="# Release {version}",
        footer=f"_完整變更記錄見 CHANGELOG.md（v{version}）。_",
    )


def render_email_banner(changelog_text: str, version: str) -> str:
    """渲染發布通知 email 的 banner body（**Markdown 格式文字**）。

    注意：body 內含逐字注入的 ``BREAKING_HEADING``（``## ⚠️ Breaking Changes``）等
    Markdown 標記，**並非 plain text**。消費端若要純文字 email，須自行將 Markdown
    render／strip（如去除前置 ``##``）後再送 SMTP；HTML email 則先過 Markdown→HTML。

    與 tag notes 共用同一抽出區塊，確保兩出口內容一致、四要素皆不遺失。
    缺區塊時拋 `MissingBreakingBlock`。
    """
    return _render(
        changelog_text,
        version,
        heading="📣 Ti Studio {version} 發布通知 — 含破壞性變更，請先閱讀",
        footer=f"— 本郵件由 release pipeline 自動產出（v{version}）。",
    )


def dry_run_dump(
    changelog_text: str,
    version: str | None = None,
    out_dir: Path | str | None = None,
) -> dict[str, str]:
    """dry-run：渲染兩出口並回傳 {'tag_notes': ..., 'email_banner': ...}。

    供 pre-tag 驗證離線比對用——不打 gh release API、不連 SMTP。
    version 省略時讀 `pyproject_version`（單一事實來源）。
    若給 out_dir，另把兩 body dump 成 `tag_notes.md` / `email_banner.txt` 供 CI artifact。
    """
    ver = version or pyproject_version()
    outputs = {
        "tag_notes": render_tag_notes(changelog_text, ver),
        "email_banner": render_email_banner(changelog_text, ver),
    }
    if out_dir is not None:
        d = Path(out_dir)
        d.mkdir(parents=True, exist_ok=True)
        (d / "tag_notes.md").write_text(outputs["tag_notes"], encoding="utf-8")
        (d / "email_banner.txt").write_text(outputs["email_banner"], encoding="utf-8")
    return outputs
