"""QA 驗收：任務 #3「orchestrator 三閘門的回報與 backlog note 加上層級標籤」。

對應驗收標準：
- orchestrator 三閘門失敗時，回報訊息與 backlog note 都帶 `[lint]`/`[collect]`/`[test]`
  其一，能一眼辨層。

策略（破壞性視角，含邊界）：
1. 行為驗證——以 ExecSpy 注入 runner.run_command_exec，實跑三閘門函式，斷言**每一條
   return 路徑**（含失敗、成功、ruff 缺失 fail-open）回傳字串都帶對應前綴標籤。
   不只測快樂路徑：lint 三條 return、collect 成敗、test 成敗全覆蓋。
2. backlog note 驗證——對真實 `run_one_task` 原始碼做 AST 解析（非硬編副本），鎖定三個
   閘門失敗分支的 `backlog.set_status(..., note=...)` 與 `backlog.add(detail=...)`，斷言
   note 帶層級標籤、detail 取自帶標籤的 gate 輸出（out[-500:]）。
3. 反向黑樣本——證明斷言有真判別力：標籤抽掉即紅（assert 前綴落在字串開頭、非任意位置）。
"""

from __future__ import annotations

import ast
import inspect

import pytest
from _repo import REPO_ROOT

from studio import autopilot, runner
from studio.runner import RunOutput

STUDIO = REPO_ROOT / "studio"


class SpyByLabel:
    """依 label 決定回傳的 exit/output；未指定者預設 ok。記錄全部呼叫。"""

    def __init__(self, results: dict[str, tuple[int, str]] | None = None):
        self.results = results or {}
        self.calls: list[dict] = []

    async def __call__(self, cwd, argv, timeout=None, sandbox=None, label=None):
        self.calls.append({"cwd": cwd, "argv": list(argv), "label": label})
        exit_code, output = self.results.get(label, (0, ""))
        return RunOutput(command=label or "", exit_code=exit_code, output=output, timed_out=False)


# ---------------------------------------------------------------------------
# 1) 閘門回傳值帶標籤——逐條 return 路徑
# ---------------------------------------------------------------------------


async def test_gate_lint_label_on_ruff_missing(monkeypatch):
    """ruff 未安裝（probe 失敗）→ fail-open True，但回報仍須帶 [lint]。"""
    spy = SpyByLabel({"ruff probe": (127, "no ruff")})
    monkeypatch.setattr(runner, "run_command_exec", spy)
    ok, out = await autopilot._gate_lint("/c")
    assert ok is True, "ruff 缺失應 fail-open"
    assert out.startswith("[lint] "), f"ruff 缺失路徑漏標籤：{out!r}"


async def test_gate_lint_label_on_check_fail(monkeypatch):
    """ruff check 失敗 → False，且失敗訊息開頭即 [lint]（失敗路徑最需辨層）。"""
    spy = SpyByLabel({"ruff check": (1, "F401 unused import")})
    monkeypatch.setattr(runner, "run_command_exec", spy)
    ok, out = await autopilot._gate_lint("/c")
    assert ok is False
    assert out.startswith("[lint] "), f"lint 失敗路徑漏標籤：{out!r}"
    assert "ruff check" in out, "失敗訊息應指明是哪個 ruff 子步驟"


async def test_gate_lint_label_on_format_fail(monkeypatch):
    """ruff format --check 失敗 → False 且帶 [lint]。"""
    spy = SpyByLabel({"ruff format --check": (1, "would reformat")})
    monkeypatch.setattr(runner, "run_command_exec", spy)
    ok, out = await autopilot._gate_lint("/c")
    assert ok is False
    assert out.startswith("[lint] ")
    assert "ruff format" in out


async def test_gate_lint_label_on_success(monkeypatch):
    """全綠 → True 且帶 [lint]（成功路徑也要標，否則前綴語意殘缺）。"""
    spy = SpyByLabel()  # 全部 ok
    monkeypatch.setattr(runner, "run_command_exec", spy)
    ok, out = await autopilot._gate_lint("/c")
    assert ok is True
    assert out.startswith("[lint] "), f"lint 成功路徑漏標籤：{out!r}"


async def test_gate_collect_label_on_fail(monkeypatch):
    spy = SpyByLabel({"collect (no SDK)": (2, "ERROR collecting tests/foo.py")})
    monkeypatch.setattr(runner, "run_command_exec", spy)
    ok, out = await autopilot._gate_collect_without_sdk("/c")
    assert ok is False
    assert out.startswith("[collect] "), f"collect 失敗路徑漏標籤：{out!r}"


async def test_gate_collect_label_on_success(monkeypatch):
    spy = SpyByLabel({"collect (no SDK)": (0, "120 tests collected")})
    monkeypatch.setattr(runner, "run_command_exec", spy)
    ok, out = await autopilot._gate_collect_without_sdk("/c")
    assert ok is True
    assert out.startswith("[collect] "), f"collect 成功路徑漏標籤：{out!r}"


async def test_gate_tests_label_on_fail(monkeypatch):
    spy = SpyByLabel({"pytest gate": (1, "1 failed")})
    monkeypatch.setattr(runner, "run_command_exec", spy)
    ok, out = await autopilot._gate_tests("/c")
    assert ok is False
    assert out.startswith("[test] "), f"test 失敗路徑漏標籤：{out!r}"


async def test_gate_tests_label_on_success(monkeypatch):
    spy = SpyByLabel({"pytest gate": (0, "5 passed")})
    monkeypatch.setattr(runner, "run_command_exec", spy)
    ok, out = await autopilot._gate_tests("/c")
    assert ok is True
    assert out.startswith("[test] "), f"test 成功路徑漏標籤：{out!r}"


# ---------------------------------------------------------------------------
# 1b) 邊界：標籤計入截尾預算，超長輸出不得讓總長爆量、尾段須保留
# ---------------------------------------------------------------------------


async def test_gate_tests_label_budget_not_exceeded(monkeypatch):
    """超長輸出時：帶標籤總長仍 ≤1500，且尾段標記保留（前綴不擠掉關鍵尾巴）。"""
    long_out = "y" * 5000 + "TAILMARK"
    spy = SpyByLabel({"pytest gate": (1, long_out)})
    monkeypatch.setattr(runner, "run_command_exec", spy)
    ok, out = await autopilot._gate_tests("/c")
    assert out.startswith("[test] ")
    assert len(out) <= 1500, f"帶標籤總長爆量：{len(out)}"
    assert out.endswith("TAILMARK"), "截尾後仍須保留輸出尾段"


# ---------------------------------------------------------------------------
# 2) backlog note 帶標籤——解析真實 run_one_task 原始碼（非硬編副本）
# ---------------------------------------------------------------------------


def _gate_failure_branches() -> list[ast.If]:
    """從 run_one_task 取出三個 `if not ok:` 閘門失敗分支（依序 lint/collect/test）。"""
    # run_one_task 為頂層函式，getsource 無前導縮排，可直接 parse（勿用 cleandoc，
    # 它會誤刪函式體相對縮排導致 IndentationError）。
    src = inspect.getsource(autopilot.run_one_task)
    tree = ast.parse(src)
    fn = tree.body[0]
    branches: list[ast.If] = []
    for node in ast.walk(fn):
        if isinstance(node, ast.If):
            # 條件為 `not ok`
            t = node.test
            if isinstance(t, ast.UnaryOp) and isinstance(t.op, ast.Not):
                if isinstance(t.operand, ast.Name) and t.operand.id == "ok":
                    branches.append(node)
    return branches


def _note_kwarg(branch: ast.If) -> str | None:
    """取分支內 backlog.set_status(...) 的 note= 字面字串。"""
    for node in ast.walk(branch):
        if isinstance(node, ast.Call):
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr == "set_status"
                and isinstance(func.value, ast.Name)
                and func.value.id == "backlog"
            ):
                for kw in node.keywords:
                    if kw.arg == "note" and isinstance(kw.value, ast.Constant):
                        return kw.value.value
    return None


def test_backlog_notes_carry_level_labels():
    """三閘門失敗分支的 set_status note 依序帶 [lint]/[collect]/[test]。"""
    branches = _gate_failure_branches()
    # run_one_task 內三個 gate（lint/collect/test）失敗分支；併合分支可能更多，但前三個
    # `not ok` 必為三閘門（merge 後續用 `not merged`，不會誤入）。
    notes = [_note_kwarg(b) for b in branches]
    notes = [n for n in notes if n is not None]
    assert len(notes) >= 3, f"未能定位三個閘門失敗 note：{notes}"
    lint_note, collect_note, test_note = notes[0], notes[1], notes[2]
    assert lint_note.startswith("[lint]"), f"lint note 漏標籤：{lint_note!r}"
    assert collect_note.startswith("[collect]"), f"collect note 漏標籤：{collect_note!r}"
    assert test_note.startswith("[test]"), f"test note 漏標籤：{test_note!r}"


def test_backlog_add_detail_from_labeled_gate_output():
    """三閘門失敗時補的修復任務 detail 取自帶標籤的 gate 輸出（out[-500:]）。

    out 已由 gate 函式加上層級前綴，故 detail 也一眼辨層。驗證源碼確以 out 切片為 detail。
    """
    branches = _gate_failure_branches()[:3]
    found = 0
    for b in branches:
        for node in ast.walk(b):
            if isinstance(node, ast.Call):
                func = node.func
                if (
                    isinstance(func, ast.Attribute)
                    and func.attr == "add"
                    and isinstance(func.value, ast.Name)
                    and func.value.id == "backlog"
                ):
                    for kw in node.keywords:
                        if kw.arg == "detail":
                            # detail=out[-500:] → Subscript on Name 'out'
                            v = kw.value
                            assert isinstance(v, ast.Subscript), ast.dump(v)
                            assert isinstance(v.value, ast.Name) and v.value.id == "out"
                            found += 1
    assert found >= 3, f"三閘門修復任務 detail 未全部取自帶標籤 gate 輸出，僅 {found}"


# ---------------------------------------------------------------------------
# 3) 反向黑樣本——證明前綴斷言有真判別力（落在開頭，非任意位置）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "label,getter,exit_code,output",
    [
        ("[lint] ", "_gate_lint", 1, "x"),  # check 走 default label 失敗
        ("[collect] ", "_gate_collect_without_sdk", 2, "x"),
        ("[test] ", "_gate_tests", 1, "x"),
    ],
)
async def test_prefix_is_at_string_head_not_buried(monkeypatch, label, getter, exit_code, output):
    """標籤必須在字串開頭：若實作只是把 [tag] 塞進字串中段，startswith 會抓到漏網。

    反向對照：把 output 設成不含任何方括號的純文字，確保 startswith 命中的是實作前綴
    而非 gate 輸出本身碰巧帶的字樣。
    """
    label_to_runlabel = {
        "_gate_lint": "ruff check",
        "_gate_collect_without_sdk": "collect (no SDK)",
        "_gate_tests": "pytest gate",
    }
    spy = SpyByLabel({label_to_runlabel[getter]: (exit_code, output)})
    monkeypatch.setattr(runner, "run_command_exec", spy)
    ok, out = await getattr(autopilot, getter)("/c")
    assert out.startswith(label), f"{getter} 前綴未落在開頭：{out!r}"
    # 黑樣本：純文字 output 不應自帶方括號標籤，確認 startswith 命中的是實作加的前綴
    assert "[" not in output
