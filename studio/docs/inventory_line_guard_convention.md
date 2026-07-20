# studio/docs inventory 行號守門慣例

目的：所有 `studio/docs/*_inventory.md` 都要先宣告自己是否把行號當契約；只要文件含現碼行號，就必須有可追蹤的行號守門狀態，避免文件靜默漂移。

## Metadata

每份 inventory 標題後需放同一段：

```md
## 行號守門

- 類型：`line-number` / `marker-only` / `historical-location`
- 狀態：`active` / `planned` / `not-required`
- 守門測試：`tests/docs/test_inventory_line_guard_<inventory-slug>.py` 或 `不適用`
- 模板：`studio/docs/inventory_line_guard_convention.md`
- 原則：文件只作被校驗方；測試須由實碼動態重算行號，不得為了過測試改產品碼或新增 wrapper。
```

類型定義：

- `line-number`：文件列出 `studio/*.py:<line>` 這類現碼行號；守門測試需動態重算。
- `marker-only`：文件用函式、類別或文字 marker 定位，不把行號當契約。
- `historical-location`：文件記錄過去盤點時的位置（例如 `L266`），不可拿來驗證現碼行號。

## 命名

行號型 inventory 的守門測試一律命名：

```text
tests/docs/test_inventory_line_guard_<inventory-slug>.py
```

`<inventory-slug>` 是 inventory 檔名移除尾端 `_inventory` 後的 stem，例如：

- `transition_await_inventory.md` -> `tests/docs/test_inventory_line_guard_transition_await.py`
- `jitter_backoff_inventory.md` -> `tests/docs/test_inventory_line_guard_jitter_backoff.py`

既有舊名測試可暫留，但新建或補強時必須用上面的正式命名。

## 測試模板

```python
from __future__ import annotations

import ast
from pathlib import Path

from _repo import REPO_ROOT

INVENTORY = REPO_ROOT / "studio" / "docs" / "<name>_inventory.md"
SOURCE = REPO_ROOT / "studio" / "<source>.py"

ANCHORS = {
    "stable_label": "unique source substring",
}


def _line_with(source: str, needle: str) -> int:
    matches = [i for i, line in enumerate(source.splitlines(), start=1) if needle in line]
    assert len(matches) == 1, f"錨點必須唯一：{needle!r} -> {matches}"
    return matches[0]


def test_inventory_line_numbers_match_live_code() -> None:
    markdown = INVENTORY.read_text(encoding="utf-8")
    source = SOURCE.read_text(encoding="utf-8")

    for needle in ANCHORS.values():
        lineno = _line_with(source, needle)
        assert f"{SOURCE.relative_to(REPO_ROOT)}:{lineno}" in markdown
```

必要原則：

- 用 AST 或唯一字串從實碼重算；不要手抄預期行號到測試。
- Markdown 是被校驗方，實碼是來源。
- 若錨點不唯一，先換更穩定的函式、類別或語意 marker。
- 守門測試不得要求產品碼新增無業務意義的 wrapper。
