import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
INVENTORY = ROOT / "studio/docs/lane_baseline_injection_inventory.md"
ORCHESTRATOR = ROOT / "studio/orchestrator.py"
RUNNER = ROOT / "studio/runner.py"
PROVIDERS = ROOT / "studio/providers.py"
DECISIONS = ROOT / "DECISIONS.md"

EXPECTED_LANE_CONTEXT_FIELDS = {
    "lane_id",
    "cwd",
    "experts",
    "critics",
    "branch",
    "last_commit",
    "notes_buffer",
}

EXPECTED_TABLE_ITEMS = [
    "主 lane context",
    "並行開關",
    "lane 切分數",
    "branch 名稱",
    "worktree 路徑",
    "base commit",
    "git worktree 啟動",
    "lane context 建立",
    "lane experts",
    "Claude expert",
    "Codex expert",
    "Antigravity expert",
    "runner 自測/指令",
    "`_integrate_wave()` 合併",
    "notes 緩衝",
    "teardown",
]


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _module(path: Path) -> ast.Module:
    return ast.parse(_read(path))


def _source_segment(path: Path, symbol: str) -> str:
    src = _read(path)
    tree = ast.parse(src)
    if "." in symbol:
        class_name, child_name = symbol.split(".", 1)
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and node.name == class_name:
                for child in node.body:
                    if (
                        isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef)
                        and child.name == child_name
                    ):
                        return ast.get_source_segment(src, child) or ""
    else:
        for node in tree.body:
            if isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
                if node.name == symbol:
                    return ast.get_source_segment(src, node) or ""
    raise AssertionError(f"source segment not found: {path}:{symbol}")


def _lane_context_fields() -> set[str]:
    for node in _module(ORCHESTRATOR).body:
        if isinstance(node, ast.ClassDef) and node.name == "LaneContext":
            return {
                child.target.id
                for child in node.body
                if isinstance(child, ast.AnnAssign) and isinstance(child.target, ast.Name)
            }
    raise AssertionError("LaneContext not found")


def _comparison_table_rows(text: str) -> list[list[str]]:
    match = re.search(r"^## 逐項對照\n(?P<body>.*?)(?=^## |\Z)", text, re.M | re.S)
    assert match, "missing comparison table section"
    rows: list[list[str]] = []
    for line in match.group("body").splitlines():
        if not line.startswith("|"):
            continue
        if set(line.replace("|", "").strip()) <= {"-", ":"}:
            continue
        rows.append([cell.strip() for cell in line.strip("|").split("|")])
    return rows


def _marker_specs(text: str) -> list[tuple[Path, str]]:
    pattern = re.compile(
        r"（`(?P<path>[^`]+)`，marker: (?P<fence>`{1,2})(?P<marker>.+?)(?P=fence)）"
    )
    return [(ROOT / match.group("path"), match.group("marker")) for match in pattern.finditer(text)]


def _python_marker_resolves(path: Path, marker: str) -> bool:
    class_match = re.fullmatch(r"class ([A-Za-z_][A-Za-z0-9_]*)", marker)
    if class_match:
        return any(
            isinstance(node, ast.ClassDef) and node.name == class_match.group(1)
            for node in ast.walk(_module(path))
        )

    func_match = re.fullmatch(r"(?:async )?def ([A-Za-z_][A-Za-z0-9_]*)", marker)
    if not func_match:
        return False
    return any(
        isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and node.name == func_match.group(1)
        for node in ast.walk(_module(path))
    )


def _function_arg_names(path: Path, name: str) -> set[str]:
    for node in ast.walk(_module(path)):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == name:
            return {
                arg.arg
                for arg in (
                    list(node.args.posonlyargs) + list(node.args.args) + list(node.args.kwonlyargs)
                )
            }
    raise AssertionError(f"function not found: {path}:{name}")


def test_inventory_records_task3_zero_drift_scope_and_handoff() -> None:
    inventory = _read(INVENTORY)

    assert "不等同已落地的 lane baseline env/manifest 契約" in inventory
    assert "再核對為零漂移" in inventory
    assert "本次僅明確標記 no-op" in inventory
    assert "`lane manifest` 層仍為前瞻契約、非現況" in inventory
    assert "現況可描述為「顯式建構參數 + process-level config + env 繼承」" in inventory
    assert "沒有 `lane manifest` 層" in inventory
    assert "落地後需補測試鎖定欄位、優先序與 fail-open/closed 策略" in inventory


def test_inventory_is_exact_live_snapshot_for_fields_and_table_shape() -> None:
    inventory = _read(INVENTORY)
    live_fields = _lane_context_fields()
    rows = _comparison_table_rows(inventory)

    assert live_fields == EXPECTED_LANE_CONTEXT_FIELDS
    assert rows[0] == ["項目", "實際來源與欄位", "env 注入", "manifest 欄位", "缺失/失敗現況"]
    data = rows[1:]
    assert len(data) == 16
    assert [row[0] for row in data] == EXPECTED_TABLE_ITEMS
    assert all(len(row) == 5 for row in data)
    assert all(row[3] == "無" for row in data)

    lane_context_line = next(
        line for line in inventory.splitlines() if line.startswith("- `LaneContext` 現況只保存")
    )
    documented_fields = set(
        re.findall(r"`([^`]+)`", lane_context_line.split("：", 1)[1].split("（", 1)[0])
    )
    assert documented_fields == live_fields


def test_inventory_markers_resolve_and_fail_examples_stay_analogies() -> None:
    inventory = _read(INVENTORY)
    markers = _marker_specs(inventory)

    assert markers
    for path, marker in markers:
        assert path.exists(), f"marker path missing: {path}"
        if path.suffix == ".py":
            assert _python_marker_resolves(path, marker), f"dead Python marker: {path}:{marker}"
        else:
            assert marker in _read(path), f"dead document marker: {path}:{marker}"

    decisions = _read(DECISIONS)
    assert "Guard 條件二選一觸發" in decisions
    assert 'DISCUSS_MODE = os.getenv("TI_DISCUSS_MODE", "legacy")' in decisions
    assert "`AUTOPILOT_REPO`" in inventory
    assert "fail-closed 類比" in inventory
    assert "`TI_DISCUSS_MODE`" in inventory
    assert "fail-open 類比" in inventory
    assert inventory.count("不是 lane baseline 現有欄位") == 2


def test_source_still_has_no_lane_baseline_env_or_manifest_injection() -> None:
    open_lane = _source_segment(ORCHESTRATOR, "StudioSession._open_lane")
    integrate_wave = _source_segment(ORCHESTRATOR, "StudioSession._integrate_wave")
    runner = _read(RUNNER)
    providers = _read(PROVIDERS)

    assert 'branch = f"lane-{self.session_id}-"' in open_lane
    assert "wt = self._lane_worktree_path(branch)" in open_lane
    assert 'base = self._last_commit or "HEAD"' in open_lane
    assert "runner.git_worktree_add(self.cwd, wt, branch, base=base)" in open_lane
    assert "LaneContext(branch, wt, {}, branch=branch)" in open_lane
    assert "env=" not in open_lane
    assert "manifest" not in open_lane.lower()

    assert "sorted(lane_results, key=lambda x: x.ctx.lane_id)" in integrate_wave
    assert "await self._run_task_in_lane(self._main_ctx, task, plan_ctx)" in integrate_wave
    assert "env" not in integrate_wave
    assert "manifest" not in integrate_wave.lower()

    assert "env" not in _function_arg_names(RUNNER, "git_worktree_add")
    assert "run_env = {**os.environ, **env} if env is not None else None" in runner
    assert "env=_codex_env()" in providers
    assert "env = os.environ.copy()" in providers
    assert "env=os.environ.copy()" in providers
