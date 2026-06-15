"""Release note 抽取器：從 CHANGELOG.md 抽出頂層 Breaking Changes 區塊。

本模組為 release pipeline 兩出口（tag notes body／email banner body）的上游資料源。
`BREAKING_HEADING` 常數是 heading 字串的**唯一事實來源**——測試 `import` 此常數即構成
CI 強制機制：常數改名或模組搬路徑會讓 import 爆炸，比 prose 文件更可靠地鎖死契約。

版本字串以 `pyproject.toml` 為單一事實來源，本模組不硬寫任何版本號。

設計契約（依架構決策）：
  - `extract_breaking_block` 為純查詢函式：找到什麼回什麼，回 None 代表「無此區塊或內容為空」。
    判斷「是否必須存在」是呼叫端（pre-tag validator）的責任，不在 extractor 內拋例外——
    避免把「本次 release 無 breaking changes（合法）」與「格式錯誤」混為一談。
  - 空區塊（strip 後為空字串）與缺區塊一律回 None，讓呼叫端只需一個 `if block is None` 分支。
  - 區塊邊界：自 `## ⚠️ Breaking Changes` 起，至下一個頂層 `## ` 止；若為 CHANGELOG 最後一個
    section（後無任何 `## `），以 `\\Z`（EOF）為界仍能抽出。
"""

# TODO: 若 email_banner 需要 HTML multipart，將 render_email_banner 拆出獨立 renderer 模組。

from __future__ import annotations

import re
import tomllib
from pathlib import Path

#: heading 字串的唯一事實來源。測試與渲染皆引用此常數，禁止硬寫字面量。
BREAKING_HEADING = "## ⚠️ Breaking Changes"

# 自 BREAKING_HEADING 起，抓到下一個頂層 `## `（行首）或 EOF（\Z）為止。
# MULTILINE 讓 ^ 逐行生效；DOTALL 讓 . 跨行；\Z 覆蓋 Breaking 為最後一個 section 的邊界。
_BLOCK_RE = re.compile(
    re.escape(BREAKING_HEADING) + r"\n(.*?)(?=^## |\Z)",
    re.DOTALL | re.MULTILINE,
)

# pyproject.toml 位於 repo 根（本檔在 studio/ 下，往上一層）。
_PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


def extract_breaking_block(text: str) -> str | None:
    """從 CHANGELOG 文字抽出頂層 Breaking Changes 區塊內容。

    回傳區塊內文（不含 heading 行，已 strip）；缺區塊或內容為空時回 None。
    """
    m = _BLOCK_RE.search(text)
    if m is None:
        return None
    body = m.group(1).strip()
    return body or None


def pyproject_version(pyproject_path: Path | None = None) -> str:
    """讀 pyproject.toml 的 project.version，作為版本字串單一事實來源。"""
    path = pyproject_path or _PYPROJECT
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    return data["project"]["version"]
