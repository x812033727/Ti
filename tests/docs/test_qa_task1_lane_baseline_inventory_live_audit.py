import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
INVENTORY = ROOT / "studio/docs/lane_baseline_injection_inventory.md"
ORCHESTRATOR = ROOT / "studio/orchestrator.py"
RUNNER = ROOT / "studio/runner.py"
PROVIDERS = ROOT / "studio/providers.py"
EXPERTS = ROOT / "studio/experts.py"

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

EXPECTED_PARENTHESIZED_SYMBOLS = {
    "StudioSession._run",
    "StudioSession._run_waves",
    "StudioSession._open_lane",
    "StudioSession._lane_worktree_path",
    "runner.git_worktree_add",
    "StudioSession._build_lane_experts",
    "experts._build_client",
    "providers.CodexExpert._run_codex",
    "providers._codex_env",
    "providers.AntigravityExpert._run_antigravity",
    "StudioSession._integrate_wave",
    "StudioSession._flush_lane_notes",
    "runner.git_worktree_remove",
}

ROW_EVIDENCE = {
    "主 lane context": ["LaneContext(", "StudioSession._run"],
    "並行開關": ["config.PARALLEL_TASKS_ENABLED", "StudioSession._run_waves"],
    "lane 切分數": ["_plan_lanes()", "PARALLEL_LANES", "LLM_MAX_CONCURRENCY"],
    "branch 名稱": ["lane-{session_id}-{task_ids}", "StudioSession._open_lane"],
    "worktree 路徑": ["<cwd>.lanes/<safe branch>", "StudioSession._lane_worktree_path"],
    "base commit": ['self._last_commit or "HEAD"', "git_worktree_add"],
    "git worktree 啟動": ["git worktree add -b", "runner.git_worktree_add"],
    "lane context 建立": ["LaneContext(branch, wt, {}, branch=branch)"],
    "lane experts": ["factory(role", "session_id", "StudioSession._build_lane_experts"],
    "Claude expert": ["ClaudeAgentOptions", "cwd=str(cwd)", "experts._build_client"],
    "Codex expert": ["env=_codex_env()", "providers.CodexExpert._run_codex"],
    "Antigravity expert": ["env=os.environ.copy()", "providers.AntigravityExpert._run_antigravity"],
    "runner 自測/指令": ["run_command_exec()", "env=None"],
    "`_integrate_wave()` 合併": ["lane_id", "StudioSession._integrate_wave"],
    "notes 緩衝": ["notes_buffer", "StudioSession._flush_lane_notes"],
    "teardown": ["git worktree remove --force", "runner.git_worktree_remove"],
}

EXPECTED_CLAUDE_EXPERT_ROW = (
    "| Claude expert | `ClaudeAgentOptions(..., hooks=..., sandbox=..., "
    "cwd=str(cwd), model=...)`（`experts._build_client`） | 未在本層設定 env | 無 | "
    "cwd 外寫入由 PreToolUse hook 擋；非 baseline manifest |"
)

EXPECTED_CLAUDE_AGENT_OPTIONS_ORDER = ["hooks", "sandbox", "cwd", "model"]


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _tree(path: Path) -> ast.Module:
    return ast.parse(_read(path))


def _lane_context_fields() -> set[str]:
    for node in _tree(ORCHESTRATOR).body:
        if isinstance(node, ast.ClassDef) and node.name == "LaneContext":
            return {
                child.target.id
                for child in node.body
                if isinstance(child, ast.AnnAssign) and isinstance(child.target, ast.Name)
            }
    raise AssertionError("找不到 LaneContext")


def _comparison_table_rows(text: str) -> list[list[str]]:
    match = re.search(r"^## 逐項對照\n(?P<body>.*?)(?=^## |\Z)", text, re.M | re.S)
    assert match, "找不到逐項對照章節"
    rows: list[list[str]] = []
    for line in match.group("body").splitlines():
        if not line.startswith("|"):
            continue
        if set(line.replace("|", "").strip()) <= {"-", ":"}:
            continue
        rows.append([cell.strip() for cell in line.strip("|").split("|")])
    return rows


def _collect_python_symbols() -> set[str]:
    sources = {
        ORCHESTRATOR: "orchestrator",
        RUNNER: "runner",
        PROVIDERS: "providers",
        EXPERTS: "experts",
    }
    symbols: set[str] = set()
    for path, module_name in sources.items():
        for node in _tree(path).body:
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                symbols.add(node.name)
                symbols.add(f"{module_name}.{node.name}")
            elif isinstance(node, ast.ClassDef):
                symbols.add(node.name)
                symbols.add(f"{module_name}.{node.name}")
                for child in node.body:
                    if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                        symbols.add(child.name)
                        symbols.add(f"{node.name}.{child.name}")
                        symbols.add(f"{module_name}.{node.name}.{child.name}")
    return symbols


def _claude_agent_options_keyword_order() -> list[str]:
    for node in ast.walk(_tree(EXPERTS)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id == "ClaudeAgentOptions":
            return [keyword.arg for keyword in node.keywords if keyword.arg is not None]
    raise AssertionError("找不到 ClaudeAgentOptions 呼叫")


def _python_marker_resolves(path: Path, marker: str) -> bool:
    tree = _tree(path)
    class_match = re.fullmatch(r"class ([A-Za-z_][A-Za-z0-9_]*)", marker)
    if class_match:
        return any(
            isinstance(node, ast.ClassDef) and node.name == class_match.group(1)
            for node in tree.body
        )

    func_match = re.fullmatch(r"(?:async )?def ([A-Za-z_][A-Za-z0-9_]*)", marker)
    if not func_match:
        return False
    name = func_match.group(1)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == name:
            return True
    return False


def test_lane_context_fields_are_exact_and_documented_from_live_code() -> None:
    text = _read(INVENTORY)
    live_fields = _lane_context_fields()
    assert live_fields == EXPECTED_LANE_CONTEXT_FIELDS

    line = next(line for line in text.splitlines() if line.startswith("- `LaneContext` 現況只保存"))
    documented = set(re.findall(r"`([^`]+)`", line.split("：", 1)[1].split("（", 1)[0]))
    assert documented == live_fields


def test_comparison_table_has_16_complete_rows_with_no_manifest_claims() -> None:
    text = _read(INVENTORY)
    rows = _comparison_table_rows(text)
    assert rows[0] == ["項目", "實際來源與欄位", "env 注入", "manifest 欄位", "缺失/失敗現況"]

    data = rows[1:]
    assert len(data) == 16
    assert [row[0] for row in data] == EXPECTED_TABLE_ITEMS
    assert len({row[0] for row in data}) == len(data), "逐項對照表不得有重複項目"
    assert all(len(row) == 5 for row in data)
    assert all(row[3] == "無" for row in data)

    by_item = {row[0]: " | ".join(row[1:]) for row in data}
    for item, snippets in ROW_EVIDENCE.items():
        for snippet in snippets:
            assert snippet in by_item[item], f"{item} 缺少佐證片段: {snippet}"


def test_claude_expert_inventory_row_matches_build_client_keyword_order() -> None:
    lines = _read(INVENTORY).splitlines()
    assert lines[26] == EXPECTED_CLAUDE_EXPERT_ROW

    keyword_order = _claude_agent_options_keyword_order()
    positions = [keyword_order.index(name) for name in EXPECTED_CLAUDE_AGENT_OPTIONS_ORDER]
    assert positions == sorted(positions)

    row = lines[26]
    row_positions = [
        row.index(f"{name}=" if name != "cwd" else "cwd=str(cwd)")
        for name in EXPECTED_CLAUDE_AGENT_OPTIONS_ORDER
    ]
    assert row_positions == sorted(row_positions)


def test_all_inventory_markers_and_parenthesized_symbols_resolve() -> None:
    text = _read(INVENTORY)
    marker_pattern = re.compile(
        r"（`(?P<path>[^`]+)`，marker: (?P<fence>`{1,2})(?P<marker>.+?)(?P=fence)）"
    )
    markers = list(marker_pattern.finditer(text))
    assert markers, "清單必須有可解析的 marker"

    for match in markers:
        path = ROOT / match.group("path")
        marker = match.group("marker")
        assert path.exists(), f"marker 指向不存在檔案: {path}"
        if path.suffix == ".py":
            assert _python_marker_resolves(path, marker), f"Python marker 無法解析: {path}:{marker}"
        else:
            assert marker in _read(path), f"文件 marker 無法解析: {path}:{marker}"

    symbols = set(re.findall(r"（`([^`]+)`）", text))
    assert symbols == EXPECTED_PARENTHESIZED_SYMBOLS

    known_symbols = _collect_python_symbols()
    for symbol in symbols:
        assert symbol in known_symbols, f"括號 symbol 無法解析到現行 Python symbol: {symbol}"


def test_current_source_still_has_no_lane_env_or_manifest_layer() -> None:
    orchestrator = _read(ORCHESTRATOR)
    runner = _read(RUNNER)
    providers = _read(PROVIDERS)
    inventory = _read(INVENTORY)

    assert "runner.git_worktree_add(self.cwd, wt, branch, base=base)" in orchestrator
    assert "LaneContext(branch, wt, {}, branch=branch)" in orchestrator
    assert "run_env = {**os.environ, **env} if env is not None else None" in runner
    assert "env=_codex_env()" in providers
    assert "env = os.environ.copy()" in providers
    assert "env=os.environ.copy()" in providers

    assert "現況可描述為「顯式建構參數 + process-level config + env 繼承」" in inventory
    assert "沒有 `lane manifest` 層" in inventory
    assert "前瞻契約" in inventory
    assert "不是 lane baseline 現有欄位" in inventory
