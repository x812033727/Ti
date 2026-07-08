"""QA 驗證（任務 #1）：過渡段 await 定位清單須對齊現行 orchestrator 實碼。

守門重點：
- 行號一律對 `studio/orchestrator.py` 現碼**動態重算**，md 為被校驗方（防文件漂移）。
- 現行過渡段入口是 `await self._integrate_wave(`；**禁用** pyc 舊符號
  `_integrate_wave_with_timeout`（產品碼不得為了過測試硬塞 wrapper）。
- 過渡段真無界 await 葉節點數量以現碼實證為準＝0（廢棄 pyc「2 個」假設）。
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INVENTORY = ROOT / "studio" / "docs" / "transition_await_inventory.md"
ORCHESTRATOR = ROOT / "studio" / "orchestrator.py"

# 過渡段錨點：以「唯一子字串」對現碼定位，回傳其 1-based 行號。
TRANSITION_ANCHORS = {
    "gather": "results = await asyncio.gather(",
    "integrate_call": "all_ok = await self._integrate_wave(",
    "integrate_def": "async def _integrate_wave(",
    "intervention": "first = await asyncio.wait_for(",
    "final_demo": "self._demo = await self._final_demo()",
}

# orchestrator 不得直接持有 subprocess 原語；一律委派 runner.*（無界葉節點＝0 的實證）。
FORBIDDEN_SUBPROCESS_PRIMITIVES = (
    "create_subprocess",
    "proc.wait(",
    ".communicate(",
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _line_with(source: str, needle: str) -> int:
    matches = [i for i, line in enumerate(source.splitlines(), start=1) if needle in line]
    assert matches, f"orchestrator 現碼找不到片段：{needle!r}"
    assert len(matches) == 1, f"片段非唯一，無法動態定位行號：{needle!r} -> {matches}"
    return matches[0]


def test_inventory_exists_and_defines_boundary():
    text = _read(INVENTORY)
    assert text.splitlines()[0].strip() == "# 過渡段 await 定位清單"
    assert "lane 全收斂 -> demo 開始" in text
    assert "起點" in text and "await asyncio.gather(" in text
    assert "主要過渡段" in text and "await self._integrate_wave(" in text
    assert "終點" in text and "_stage_demo" in text


def test_no_pyc_wrapper_symbol_anywhere():
    # 產品碼不得為遷就 pyc 死符號硬塞 wrapper；md 也不得沿用舊字串。
    assert "_integrate_wave_with_timeout" not in _read(ORCHESTRATOR)
    assert "_integrate_wave_with_timeout" not in _read(INVENTORY)


def test_unbounded_leaf_count_is_zero_and_subprocess_delegated():
    md = _read(INVENTORY)
    src = _read(ORCHESTRATOR)

    # md 如實記 0，並宣告過渡段 subprocess 收尾均帶 timeout。
    assert "0 個" in md
    assert "過渡段 subprocess 收尾均帶 timeout" in md
    assert "真無界 await 葉節點數量為 **0**" in md

    # 實證：orchestrator 全檔無裸 subprocess 原語，故過渡段無界葉節點＝0。
    leaked = [p for p in FORBIDDEN_SUBPROCESS_PRIMITIVES if p in src]
    assert not leaked, f"orchestrator 不應直接持有 subprocess 原語：{leaked}"


def test_await_table_rows_are_machine_checkable():
    rows = [r for r in _read(INVENTORY).splitlines() if r.startswith("| studio/orchestrator.py:")]
    assert rows, "缺少可機器檢查的 await 表格列"
    for row in rows:
        # 位置／await／型別／現有 timeout／判定 共五欄，以 `|` 分隔。
        cols = [c.strip() for c in row.strip().strip("|").split("|")]
        assert len(cols) == 5, f"表格列須含五欄：{row}"


def test_inventory_covers_current_transition_await_lines():
    md = _read(INVENTORY)
    src = _read(ORCHESTRATOR)

    required = {needle: _line_with(src, needle) for needle in TRANSITION_ANCHORS.values()}
    missing = [
        f"{needle} @ studio/orchestrator.py:{lineno}"
        for needle, lineno in required.items()
        if f"studio/orchestrator.py:{lineno}" not in md
    ]
    assert not missing, f"定位清單未涵蓋現行過渡段 await 行號：{missing}"
