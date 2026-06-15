#!/usr/bin/env python3
"""薄包裝：讀 CHANGELOG.md → render_tag_notes(text, pyproject_version()) → 寫 body.md。

供 `.github/workflows/publish-release.yml` 的 "Render release body" step 執行。
render 失敗（如 MissingBreakingBlock）直接 raise 非零退出；採「先 build 後 write」，
失敗時不會寫出半截 body.md，亦不殘留舊內容供 "Create release" step 誤讀。
"""

from __future__ import annotations

import sys
from pathlib import Path

# 以 `python scripts/publish_release.py` 執行時，sys.path 只含 scripts/ 而非 repo root，
# `import studio` 會失敗。明確把 repo root（本檔上一層）加入 path，使腳本不依賴呼叫方式。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from studio.release_note import pyproject_version, render_tag_notes  # noqa: E402

BODY_PATH = Path("body.md")


def build_body() -> str:
    """讀真實 CHANGELOG.md + pyproject 版本（SSOT），渲染 release body 文字。"""
    changelog = Path("CHANGELOG.md").read_text(encoding="utf-8")
    return render_tag_notes(changelog, pyproject_version())


def main() -> None:
    BODY_PATH.write_text(build_body(), encoding="utf-8")


if __name__ == "__main__":
    main()
