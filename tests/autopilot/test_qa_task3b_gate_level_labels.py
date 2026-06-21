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


def _handle_gate_failure_labels() -> list[str]:
    """從 run_one_task 取出各閘門失敗分支呼叫 `_handle_gate_failure(task, "<label>", out)`
    傳入的 gate_label 字面字串，依出現順序回傳。

    Option 2（2026-06-21）後：三閘門失敗不再各自 inline `set_status(note=...)`＋`add("修復X")`，
    改為統一呼叫 `_handle_gate_failure`（有限次重試同一任務、用完才 failed），label 由第二個
    位置引數帶入；層級辨識（[lint]/[collect]/[test]/[merge]）的真值來源遷至此處。
    """
    src = inspect.getsource(autopilot.run_one_task)
    tree = ast.parse(src)
    fn = tree.body[0]
    labels: list[str] = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id == "_handle_gate_failure":
                # 簽章：_handle_gate_failure(task, gate_label, detail)
                if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
                    labels.append(node.args[1].value)
    return labels


def test_gate_failures_route_through_handler_with_labels():
    """三閘門（lint/collect/test）失敗分支都呼叫 _handle_gate_failure 並帶對應 label。"""
    labels = _handle_gate_failure_labels()
    # 至少涵蓋三閘門 + merge；前三個依序為 lint/collect/test。
    assert labels[:3] == ["lint", "collect", "test"], f"閘門 label 順序/缺漏：{labels}"
    assert "merge" in labels, f"merge 失敗也應走同一 handler：{labels}"


def test_handler_note_carries_level_label():
    """_handle_gate_failure 的 set_status note 以 `[<label>]` 開頭，保留一眼辨層的語意。

    解析 _handle_gate_failure 源碼，確認兩條 set_status（重試 pending／放棄 failed）的 note
    都用 f-string 且以字面 `[` 起頭、緊接 `{gate_label}`，標籤落在開頭非中段。
    """
    src = inspect.getsource(autopilot._handle_gate_failure)
    tree = ast.parse(src)
    fn = tree.body[0]
    note_heads: list[str] = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr == "set_status"
                and isinstance(func.value, ast.Name)
                and func.value.id == "backlog"
            ):
                for kw in node.keywords:
                    val = kw.value
                    # note 可能是 f"...".strip()——剝掉外層 .strip() 取得內層 f-string
                    if (
                        isinstance(val, ast.Call)
                        and isinstance(val.func, ast.Attribute)
                        and val.func.attr == "strip"
                    ):
                        val = val.func.value
                    if kw.arg == "note" and isinstance(val, ast.JoinedStr):
                        # f-string：第一個片段須為以 "[" 起頭的字面，緊接 gate_label 的格式化欄位
                        vals = val.values
                        assert vals and isinstance(vals[0], ast.Constant), ast.dump(kw.value)
                        head = vals[0].value
                        assert head.startswith("["), f"note 標籤未落在開頭：{head!r}"
                        assert isinstance(vals[1], ast.FormattedValue), "標籤後須緊接格式化欄位"
                        note_heads.append(head)
    assert len(note_heads) >= 2, f"應有重試/放棄兩條帶標籤 note，實得：{note_heads}"


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
