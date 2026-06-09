"""QA 驗收：任務 #1「`run_command` shell 呼叫端遷移清冊」。

驗收標準 #1：存在一份呼叫端清冊（含檔案:行、分類 a/c、理由），涵蓋全部 5 處
（程式碼層級為 9 行 shell 呼叫，歸併為 5 個邏輯呼叫端）且分類正確。

本測試不改動產品程式碼，純粹「以清冊為真相、與實際 codebase 交叉比對」：
- 清冊檔存在、表格可解析、每列具備檔案:行/分類/理由。
- 清冊引用的每個 file:line 確實是 shell 版 `run_command(`（非 `run_command_exec`）。
- 全域 grep 出的所有 shell `run_command` 呼叫端，皆被清冊涵蓋（無遺漏）。
- 分類正確：a 類為固定字面字串（無動態變數）、c 類為動態/使用者輸入。
"""

from __future__ import annotations

import re

import pytest
from _repo import REPO_ROOT

ROOT = REPO_ROOT
STUDIO = ROOT / "studio"
INVENTORY = STUDIO / "docs" / "subprocess_migration_inventory.md"

# 期望的 5 個邏輯呼叫端 → (分類, 涵蓋的程式碼行集合)
# 註：a 類為「遷移目標」，會在 PR2/PR3 陸續改為 run_command_exec（行號隨之漂移，
# 已更新為遷移後位置）；c 類永久保留 shell。
EXPECTED = {
    "runner.py git init/config": ("a", {296, 299, 303, 307}),
    "runner.py git_clone": ("a", {347}),
    "autopilot.py pytest gate": ("a", {93}),
    "orchestrator.py demo/self-test": ("c", {1046, 1065}),
    "tools.py run_bash": ("c", {133}),
}


def _grep_shell_callers() -> dict[str, set[int]]:
    """掃出 codebase 內所有 shell 版 run_command 呼叫端（排除 exec / 定義 / 解析輔助）。"""
    pat = re.compile(r"run_command\(")
    skip = re.compile(r"run_command_exec|def run_command|parse_run_command|resolve_demo_command")
    found: dict[str, set[int]] = {}
    for py in STUDIO.rglob("*.py"):
        for i, line in enumerate(py.read_text(encoding="utf-8").splitlines(), 1):
            if pat.search(line) and not skip.search(line):
                found.setdefault(py.name, set()).add(i)
    return found


def _grep_all_callers() -> dict[str, set[int]]:
    """掃出所有 run_command / run_command_exec 呼叫端（含已遷移 exec）。

    清冊是「遷移工作清單」：a 類呼叫端遷移後變成 exec，仍應被清冊涵蓋且非虛構。
    故 phantom 檢查須認得 shell 與 exec 兩種形態。
    """
    pat = re.compile(r"run_command(_exec)?\(")
    skip = re.compile(r"def run_command|parse_run_command|resolve_demo_command")
    found: dict[str, set[int]] = {}
    for py in STUDIO.rglob("*.py"):
        for i, line in enumerate(py.read_text(encoding="utf-8").splitlines(), 1):
            if pat.search(line) and not skip.search(line):
                found.setdefault(py.name, set()).add(i)
    return found


@pytest.fixture(scope="module")
def inventory_text() -> str:
    assert INVENTORY.exists(), f"清冊檔不存在：{INVENTORY}"
    text = INVENTORY.read_text(encoding="utf-8")
    assert text.strip(), "清冊檔為空"
    return text


@pytest.fixture(scope="module")
def inventory_rows(inventory_text: str) -> list[dict]:
    """解析 markdown 表格列，回傳 [{file, lines:set, cls, reason}]。"""
    rows = []
    # 期望表頭：# | 檔案:行 | 內容 | 分類 | 理由 | 遷移注意
    for raw in inventory_text.splitlines():
        if not raw.strip().startswith("|"):
            continue
        cells = [c.strip() for c in raw.strip().strip("|").split("|")]
        if len(cells) < 5 or cells[0] in ("#", "") or set(cells[0]) <= {"-", ":"}:
            continue
        if not cells[0].isdigit():
            continue
        file_line = cells[1]  # 例：`runner.py:267-273` (`git_clone`)
        m = re.search(r"([\w.]+\.py):([\d,\-]+)", file_line)
        assert m, f"清冊列無法解析檔案:行 → {file_line!r}"
        fname = m.group(1)
        lines: set[int] = set()
        for part in m.group(2).split(","):
            if "-" in part:
                a, b = part.split("-")
                lines.update(range(int(a), int(b) + 1))
            else:
                lines.add(int(part))
        cls_cell = cells[3].lower()
        cls = (
            "a" if re.search(r"\ba\b", cls_cell) else ("c" if re.search(r"\bc\b", cls_cell) else "")
        )
        rows.append(
            {"file": fname, "lines": lines, "cls": cls, "reason": cells[4], "raw_lines": file_line}
        )
    assert rows, "清冊未解析到任何資料列"
    return rows


# --- 驗收 1：清冊結構完整 ------------------------------------------------


def test_inventory_exists_and_has_template_basis(inventory_text: str):
    """清冊存在且明示基準依據為 publisher.py argv pattern。"""
    assert "publisher" in inventory_text, "清冊未載明 publisher.py 基準範本"
    assert "run_command_exec" in inventory_text, "清冊未說明 exec 雙路徑"


def test_every_row_has_classification_and_reason(inventory_rows: list[dict]):
    for row in inventory_rows:
        assert row["cls"] in ("a", "c"), f"列 {row['raw_lines']} 分類非 a/c：{row['cls']!r}"
        assert len(row["reason"]) >= 5, f"列 {row['raw_lines']} 缺理由"


# --- 驗收 1：涵蓋全部呼叫端、無遺漏 -------------------------------------


def test_inventory_covers_all_shell_callers(inventory_rows: list[dict]):
    """codebase 內每個 shell run_command 呼叫端都被清冊涵蓋。"""
    actual = _grep_shell_callers()
    # 清冊涵蓋的 file -> lines
    covered: dict[str, set[int]] = {}
    for row in inventory_rows:
        covered.setdefault(row["file"], set()).update(row["lines"])

    missing = []
    for fname, lines in actual.items():
        not_covered = lines - covered.get(fname, set())
        if not_covered:
            missing.append(f"{fname}:{sorted(not_covered)}")
    assert not missing, f"清冊未涵蓋的 shell 呼叫端：{missing}"


def test_no_phantom_rows(inventory_rows: list[dict]):
    """清冊每列（含範圍標記）至少對應一個實際 shell run_command 呼叫，無虛構列。

    清冊允許用區間（如 269-280）框住一段固定 git init/config 區塊，區間內的
    if/return/註解行不算呼叫端；只要區間命中至少一個真實呼叫即視為有效。
    （認得 shell 與已遷移的 exec 兩種形態——a 類遷移後不應被誤判為虛構列。）
    """
    actual = _grep_all_callers()
    phantom = []
    for row in inventory_rows:
        hit = row["lines"] & actual.get(row["file"], set())
        if not hit:
            phantom.append(f"{row['file']}:{row['raw_lines']}")
    assert not phantom, f"清冊列在程式碼中找不到對應 shell run_command：{phantom}"


# --- 驗收 1：分類正確 ---------------------------------------------------


@pytest.mark.parametrize("name", list(EXPECTED.keys()))
def test_expected_caller_present_with_correct_class(name: str, inventory_rows: list[dict]):
    """5 個邏輯呼叫端皆在清冊，且分類符合預期。"""
    want_cls, want_lines = EXPECTED[name]
    # 找出涵蓋這些行的清冊列
    matched = [
        r for r in inventory_rows if (r["lines"] & want_lines) and r["file"] == name.split()[0]
    ]
    assert matched, f"清冊缺少呼叫端：{name}（行 {sorted(want_lines)}）"
    for r in matched:
        assert r["cls"] == want_cls, (
            f"{name} 分類錯誤：清冊={r['cls']} 期望={want_cls}（行 {r['raw_lines']}）"
        )


def test_classification_matches_code_reality():
    """以程式碼事實驗證分類（遷移感知）。

    - c 類：必須仍是 shell `run_command(` 且傳入動態變數（保留 shell 的理由成立）。
    - a 類：不得是「動態輸入的 shell run_command」；允許兩種合法狀態——
      已遷移（`run_command_exec` + argv）或待遷移（shell `run_command` + 固定字面字串）。
    本測試對遷移過程的任何中間態（runner 已遷移、autopilot 待遷移）皆綠。
    """
    runner_src = (STUDIO / "runner.py").read_text(encoding="utf-8")
    autopilot_src = (STUDIO / "autopilot.py").read_text(encoding="utf-8")
    orch = (STUDIO / "orchestrator.py").read_text(encoding="utf-8").splitlines()
    tools = (STUDIO / "tools.py").read_text(encoding="utf-8").splitlines()

    # --- a 類 runner：固定 git 指令必須已走 exec，且不得殘留 shell run_command ---
    # git init / config / clone 皆以 run_command_exec(argv) 執行。
    for argv_head in ('["git", "init"', '["git", "config"', "git clone"):
        assert argv_head in runner_src, f"runner.py 預期含已遷移的 git argv：{argv_head}"
    # runner.py 不應再有任何 shell 版 run_command 呼叫（git 指令已全部 exec 化）。
    shell_calls = re.findall(r"(?<!_exec)\brun_command\(", runner_src)
    # 排除函式定義本身（def run_command( / def run_command_exec(）。
    def_calls = len(re.findall(r"def run_command\(", runner_src))
    assert len(shell_calls) == def_calls, (
        f"runner.py 仍殘留 shell run_command 呼叫端（應全部遷移為 exec）：{len(shell_calls) - def_calls} 處"
    )

    # --- a 類 autopilot：pytest gate 為固定指令，shell（待遷移）或 exec（已遷移）皆可 ---
    assert (
        '"python -m pytest -q"' in autopilot_src  # 待遷移：shell 字串
        or '"-m", "pytest", "-q"'
        in autopilot_src  # 已遷移：exec argv（python / sys.executable 皆可）
    ), "autopilot pytest gate 應為固定 pytest 指令 (a 類，shell 或 exec 皆可)"

    # --- c 類：傳入的是變數（cmd / args.get(...)），必須仍走 shell run_command ---
    # _self_test 走 lane context（ctx.cwd）、_final_demo 走整體 workspace（self.cwd），皆傳動態 cmd。
    for ln in (1046, 1065):
        assert re.search(r"run_command\((?:ctx|self)\.cwd, cmd\)", orch[ln - 1]), (
            f"orchestrator.py:{ln} 預期傳動態 cmd 變數、保留 shell (c 類)"
        )
    assert 'args.get("command"' in tools[132], "tools.py:133 預期傳使用者輸入、保留 shell (c 類)"


def test_logical_caller_count_is_five(inventory_rows: list[dict]):
    """歸併後恰為 5 個邏輯呼叫端（a:3 / c:2）。"""
    logical = set()
    for r in inventory_rows:
        for key, (_cls, lines) in EXPECTED.items():
            if r["file"] == key.split()[0] and (r["lines"] & lines):
                logical.add(key)
    assert logical == set(EXPECTED), f"邏輯呼叫端不齊：{set(EXPECTED) - logical}"
    a_cnt = sum(1 for v in EXPECTED.values() if v[0] == "a")
    c_cnt = sum(1 for v in EXPECTED.values() if v[0] == "c")
    assert (a_cnt, c_cnt) == (3, 2), f"分類比例異常 a={a_cnt} c={c_cnt}"
