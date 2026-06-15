#!/usr/bin/env python3
"""release body 取值介面（task #2）。

職責單一：讀 ``CHANGELOG.md`` → ``render_tag_notes(text, pyproject_version())``
→ 產出 release body，供 publish-release.yml 的「建立 release」step 使用。

兩個出口（都來自同一份 render 結果，零字串拼裝）：
  1. ``body.md`` 檔案 —— 供 ``gh release create <tag> -F body.md`` 讀取（file mode，
     避免多行 / 反斜線 / ``-`` 開頭被 shell 吞字）。
  2. ``$GITHUB_OUTPUT`` 的 ``body`` 鍵 —— 比照 release-smoke.yml 既有 BODY 慣例，
     多行值用隨機分隔符寫入，供下游 step 以 env 取值 / debug。

版本字串一律走 ``studio.release_note.pyproject_version`` 單一事實來源，本檔不硬寫版本；
heading 字面值一律由 ``studio.release_note`` 注入，本檔零 Breaking heading 字面值。

render 失敗（缺 Breaking 區塊）時 ``render_tag_notes`` 拋 ``MissingBreakingBlock``，
例外向上拋出 → 非零退出，且**先刪除舊 body.md** 再 render，確保失敗後不殘留舊內容
供下游 step 誤讀（冪等性保障，依架構決策）。
"""

from __future__ import annotations

import os
import secrets
import sys
from pathlib import Path

# 本檔在 scripts/ 下，repo 根為上一層。直接 `python scripts/publish_release.py`
# 執行時，把 repo 根加進 sys.path 才能 import studio 套件。
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from studio.release_note import pyproject_version, render_tag_notes  # noqa: E402


def render_release_body(
    changelog_path: Path | str | None = None,
    version: str | None = None,
) -> str:
    """讀 CHANGELOG 並渲染 release body 字串。

    ``changelog_path`` 省略時讀 repo 根的 ``CHANGELOG.md``；
    ``version`` 省略時走 ``pyproject_version()`` 單一事實來源。
    缺 Breaking 區塊時由 ``render_tag_notes`` 拋 ``MissingBreakingBlock``。
    """
    path = Path(changelog_path) if changelog_path else _ROOT / "CHANGELOG.md"
    text = path.read_text(encoding="utf-8")
    ver = version or pyproject_version()
    return render_tag_notes(text, ver)


def write_github_output(name: str, value: str, output_path: Path | str) -> None:
    """比照 release-smoke.yml：多行值以隨機分隔符寫入 GITHUB_OUTPUT。

    隨機分隔符（``secrets.token_hex``）避免 body 內容含固定分隔字串造成截斷／注入。
    """
    delim = f"{name.upper()}_EOF_{secrets.token_hex(16)}"
    with open(output_path, "a", encoding="utf-8") as fh:
        fh.write(f"{name}<<{delim}\n{value}\n{delim}\n")


def main() -> int:
    body_md = _ROOT / "body.md"
    # 先清掉舊檔：render 失敗時例外會在寫檔前拋出，body.md 不殘留舊內容供下游誤讀。
    body_md.unlink(missing_ok=True)

    body = render_release_body()  # 缺區塊 → MissingBreakingBlock → 非零退出
    body_md.write_text(body, encoding="utf-8")

    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        write_github_output("body", body, out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
