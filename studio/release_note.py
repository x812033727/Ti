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
