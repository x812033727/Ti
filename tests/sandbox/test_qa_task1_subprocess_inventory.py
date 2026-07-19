"""QA 驗收：任務 #1「`run_command` shell 呼叫端遷移清冊」。

驗收標準 #1：存在一份呼叫端清冊（含檔案·錨點、分類 a/c、理由），涵蓋全部 5 處
（程式碼層級為 9 行 shell/exec 呼叫，歸併為 5 個邏輯呼叫端）且分類正確。

呼叫端以「錨點（檔案 · 函式名）」釘選，**不以絕對行號**——任何在呼叫端之前插入
的無關程式碼都不會位移錨點、不會誤觸本測試（見 issue #78：行號釘選導致動到
orchestrator 上半部的無關 PR 反复誤紅，只能手動 bookkeeping 行號才轉綠）。

本測試不改動產品程式碼，純粹「以清冊為真相、與實際 codebase 交叉比對」：
- 清冊檔存在、表格可解析、每列具備檔案::錨點/分類/理由。
- 清冊引用的每個 (檔案, 函式) 錨點確實含實際 `run_command(` / `run_command_exec(` 呼叫端。
- 全域 AST 掃出的所有 shell `run_command` 呼叫端（依函式錨點歸併），皆被清冊涵蓋（無遺漏）。
- 分類正確：a 類為固定字面字串（已遷移 exec 或待遷移 shell）、c 類為動態/使用者輸入（保留 shell）。
"""

from __future__ import annotations

import ast
import re
from typing import NamedTuple

import pytest
from _repo import REPO_ROOT

ROOT = REPO_ROOT
STUDIO = ROOT / "studio"
INVENTORY = STUDIO / "docs" / "subprocess_migration_inventory.md"

# 期望的 5 個邏輯呼叫端 → (分類, {(檔案, 函式錨點), ...})
# 註：a 類為「遷移目標」，已在 PR2/PR3 改為 run_command_exec；c 類永久保留 shell。
# 以「函式錨點」（非行號）釘選——orchestrator/runner 上半部插入無關程式碼皆不影響本表，
# 不再需要每次行漂移就手動更新（issue #78）。
EXPECTED: dict[str, tuple[str, set[tuple[str, str]]]] = {
    "runner.py git init/config": ("a", {("runner.py", "git_init")}),
    "runner.py git_clone": ("a", {("runner.py", "git_clone")}),
    "autopilot.py pytest gate": ("a", {("autopilot.py", "_gate_tests")}),
    "orchestrator.py demo/self-test": (
        "c",
        {("orchestrator.py", "_self_test"), ("orchestrator.py", "_final_demo")},
    ),
    "tools.py run_bash": ("c", {("tools.py", "execute")}),
}


class Caller(NamedTuple):
    file: str  # 檔名（如 orchestrator.py）
    anchor: str  # 最內層包覆函式名（如 _self_test）
    is_exec: bool  # True=run_command_exec（已遷移 argv）/ False=shell run_command
    lineno: int  # 實際行號（僅供錯誤訊息參考，不作釘選用）
    src: str  # 呼叫所在原始碼行（去前後空白）


def _anchor_of(funcs: list[tuple[str, int, int]], lineno: int) -> str:
    """回傳包覆 lineno 的最內層函式名（以起始行最大者為內層）；不在任何函式內回 <module>。"""
    best: tuple[str, int, int] | None = None
    for name, start, end in funcs:
        if start <= lineno <= end and (best is None or start > best[1]):
            best = (name, start, end)
    return best[0] if best else "<module>"


def _iter_callers() -> list[Caller]:
    """AST 掃出 studio/ 內所有 `run_command` / `run_command_exec` 呼叫端。

    以 callee 名稱精準辨識——不會像純文字 grep 那樣誤命中
    `parse_run_command(` / `resolve_demo_command(` 子字串，也不含 `def` 定義本身。
    """
    callers: list[Caller] = []
    for py in sorted(STUDIO.rglob("*.py")):
        src = py.read_text(encoding="utf-8")
        tree = ast.parse(src)
        funcs = [
            (n.name, n.lineno, n.end_lineno or n.lineno)
            for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)
        ]
        lines = src.splitlines()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute):
                callee = func.attr  # runner.run_command(...)
            elif isinstance(func, ast.Name):
                callee = func.id  # run_command_exec(...)（runner.py 內裸呼叫）
            else:
                continue
            if callee not in ("run_command", "run_command_exec"):
                continue
            callers.append(
                Caller(
                    file=py.name,
                    anchor=_anchor_of(funcs, node.lineno),
                    is_exec=(callee == "run_command_exec"),
                    lineno=node.lineno,
                    src=lines[node.lineno - 1].strip(),
                )
            )
    return callers


def _shell_anchors() -> set[tuple[str, str]]:
    """所有 shell 版 `run_command` 呼叫端的 (檔案, 函式) 錨點集合。"""
    return {(c.file, c.anchor) for c in _iter_callers() if not c.is_exec}


def _all_anchors() -> set[tuple[str, str]]:
    """所有 `run_command` / `run_command_exec` 呼叫端的 (檔案, 函式) 錨點集合（含已遷移 exec）。

    清冊是「遷移工作清單」：a 類呼叫端遷移後變成 exec，仍應被清冊涵蓋且非虛構，
    故 phantom 檢查須認得 shell 與 exec 兩種形態。
    """
    return {(c.file, c.anchor) for c in _iter_callers()}


@pytest.fixture(scope="module")
def inventory_text() -> str:
    assert INVENTORY.exists(), f"清冊檔不存在：{INVENTORY}"
    text = INVENTORY.read_text(encoding="utf-8")
    assert text.strip(), "清冊檔為空"
    return text


@pytest.fixture(scope="module")
def inventory_rows(inventory_text: str) -> list[dict]:
    """解析 markdown 表格列，回傳 [{file, anchor, cls, reason, raw}]。"""
    rows = []
    # 期望表頭：# | 檔案::錨點 | 內容 | 分類 | 理由 | 遷移注意
    for raw in inventory_text.splitlines():
        if not raw.strip().startswith("|"):
            continue
        cells = [c.strip() for c in raw.strip().strip("|").split("|")]
        if len(cells) < 5 or cells[0] in ("#", "") or set(cells[0]) <= {"-", ":"}:
            continue
        if not cells[0].isdigit():
            continue
        loc = cells[1]  # 例：`orchestrator.py::_self_test`
        m = re.search(r"([\w.]+\.py)::(\w+)", loc)
        assert m, f"清冊列無法解析檔案::錨點 → {loc!r}"
        cls_cell = cells[3].lower()
        cls = (
            "a" if re.search(r"\ba\b", cls_cell) else ("c" if re.search(r"\bc\b", cls_cell) else "")
        )
        rows.append(
            {"file": m.group(1), "anchor": m.group(2), "cls": cls, "reason": cells[4], "raw": loc}
        )
    assert rows, "清冊未解析到任何資料列"
    return rows


# --- 驗收 1：清冊結構完整 ------------------------------------------------


def test_inventory_exists_and_has_template_basis(inventory_text: str):
    """清冊存在且明示基準依據為 publisher.py argv pattern。"""
    assert "publisher" in inventory_text, "清冊未載明 publisher.py 基準範本"
    assert "run_command_exec" in inventory_text, "清冊未說明 exec 雙路徑"


def test_inventory_uses_function_anchors_not_line_numbers(inventory_rows: list[dict]):
    """清冊以函式錨點釘選，不得退回絕對行號（issue #78 的回歸防線）。

    每列定位欄須是 `檔案.py::函式名` 形態；若有人「順手」把行號加回去
    （如 `檔案.py:1283`），錨點欄就不再以 `::函式名` 結尾，本測試攔下。
    """
    bad = [r["raw"] for r in inventory_rows if not re.fullmatch(r"\w+", r["anchor"])]
    assert not bad, f"清冊定位欄未使用函式錨點（疑似殘留行號）：{bad}"


def test_every_row_has_classification_and_reason(inventory_rows: list[dict]):
    for row in inventory_rows:
        assert row["cls"] in ("a", "c"), f"列 {row['raw']} 分類非 a/c：{row['cls']!r}"
        assert len(row["reason"]) >= 5, f"列 {row['raw']} 缺理由"


# --- 驗收 1：涵蓋全部呼叫端、無遺漏 -------------------------------------


def test_inventory_covers_all_shell_callers(inventory_rows: list[dict]):
    """codebase 內每個 shell run_command 呼叫端（依函式錨點）都被清冊涵蓋。"""
    actual = _shell_anchors()
    covered = {(r["file"], r["anchor"]) for r in inventory_rows}
    missing = sorted(actual - covered)
    assert not missing, f"清冊未涵蓋的 shell 呼叫端錨點：{missing}"


def test_no_phantom_rows(inventory_rows: list[dict]):
    """清冊每列的錨點都對應一個實際 run_command 呼叫，無虛構列。

    認得 shell 與已遷移的 exec 兩種形態——a 類遷移後不應被誤判為虛構列。
    """
    actual = _all_anchors()
    phantom = sorted(
        (r["file"], r["anchor"]) for r in inventory_rows if (r["file"], r["anchor"]) not in actual
    )
    assert not phantom, f"清冊列在程式碼中找不到對應 run_command 錨點：{phantom}"


# --- 驗收 1：分類正確 ---------------------------------------------------


@pytest.mark.parametrize("name", list(EXPECTED.keys()))
def test_expected_caller_present_with_correct_class(name: str, inventory_rows: list[dict]):
    """5 個邏輯呼叫端的每個錨點皆在清冊，且分類符合預期。"""
    want_cls, want_anchors = EXPECTED[name]
    matched = [r for r in inventory_rows if (r["file"], r["anchor"]) in want_anchors]
    matched_anchors = {(r["file"], r["anchor"]) for r in matched}
    assert matched_anchors == want_anchors, (
        f"清冊缺少呼叫端：{name}（缺錨點 {sorted(want_anchors - matched_anchors)}）"
    )
    for r in matched:
        assert r["cls"] == want_cls, (
            f"{name} 分類錯誤：清冊={r['cls']} 期望={want_cls}（{r['raw']}）"
        )


def test_classification_matches_code_reality():
    """以程式碼事實驗證分類（遷移感知、依錨點定位、不寫死行號）。

    - c 類：必須仍是 shell `run_command(` 且傳入動態變數（保留 shell 的理由成立）。
    - a 類：不得是「動態輸入的 shell run_command」；允許兩種合法狀態——
      已遷移（`run_command_exec` + argv）或待遷移（shell `run_command` + 固定字面字串）。
    本測試對遷移過程的任何中間態（runner 已遷移、autopilot 待遷移）皆綠。
    """
    callers = _iter_callers()
    runner_src = (STUDIO / "runner.py").read_text(encoding="utf-8")
    autopilot_src = (STUDIO / "autopilot.py").read_text(encoding="utf-8")

    # --- a 類 runner：固定 git 指令必須已走 exec，且不得殘留 shell run_command ---
    # git init / config / clone 皆以 run_command_exec(argv) 執行。
    for argv_head in ('["git", "init"', '["git", "config"', "git clone"):
        assert argv_head in runner_src, f"runner.py 預期含已遷移的 git argv：{argv_head}"
    # runner.py 不應再有任何 shell 版 run_command 呼叫端（git 指令已全部 exec 化）。
    shell_in_runner = [c for c in callers if c.file == "runner.py" and not c.is_exec]
    assert not shell_in_runner, (
        f"runner.py 仍殘留 shell run_command 呼叫端（應全部遷移為 exec）："
        f"{[(c.anchor, c.lineno) for c in shell_in_runner]}"
    )

    # --- a 類 autopilot：pytest gate 為固定指令，shell（待遷移）或 exec（已遷移）皆可 ---
    assert (
        '"python -m pytest -q"' in autopilot_src  # 待遷移：shell 字串
        or '"-m", "pytest", "-q"'
        in autopilot_src  # 已遷移：exec argv（python / sys.executable 皆可）
    ), "autopilot pytest gate 應為固定 pytest 指令 (a 類，shell 或 exec 皆可)"

    # --- c 類：傳入的是變數（cmd / args.get(...)），必須仍走 shell run_command ---
    # _self_test 走 lane context（ctx.cwd）、_final_demo 走整體 workspace（self.cwd），皆傳動態
    # cmd；_final_demo 另有 usage-error 消毒重試呼叫端（傳 sanitized，同為動態 c 類）。
    for anchor in ("_self_test", "_final_demo"):
        hits = [
            c
            for c in callers
            if c.file == "orchestrator.py" and c.anchor == anchor and not c.is_exec
        ]
        assert hits, f"orchestrator.py::{anchor} 預期為 shell run_command (c 類)，未找到"
        for c in hits:
            assert re.search(r"run_command\((?:ctx|self)\.cwd, (?:cmd|sanitized)\)", c.src), (
                f"orchestrator.py::{anchor} (L{c.lineno}) 預期傳動態 cmd 變數、保留 shell (c 類)：{c.src!r}"
            )
    # tools.py run_bash：傳使用者輸入（args.get("command", ...)），必須仍走 shell run_command。
    bash_hits = [
        c for c in callers if c.file == "tools.py" and c.anchor == "execute" and not c.is_exec
    ]
    assert bash_hits, "tools.py::execute 預期含 shell run_command (c 類)，未找到"
    assert any('args.get("command"' in c.src for c in bash_hits), (
        "tools.py run_bash 預期傳使用者輸入、保留 shell (c 類)"
    )


def test_logical_caller_count_is_five(inventory_rows: list[dict]):
    """歸併後恰為 5 個邏輯呼叫端（a:3 / c:2）。"""
    logical = set()
    for r in inventory_rows:
        for key, (_cls, anchors) in EXPECTED.items():
            if (r["file"], r["anchor"]) in anchors:
                logical.add(key)
    assert logical == set(EXPECTED), f"邏輯呼叫端不齊：{set(EXPECTED) - logical}"
    a_cnt = sum(1 for v in EXPECTED.values() if v[0] == "a")
    c_cnt = sum(1 for v in EXPECTED.values() if v[0] == "c")
    assert (a_cnt, c_cnt) == (3, 2), f"分類比例異常 a={a_cnt} c={c_cnt}"
