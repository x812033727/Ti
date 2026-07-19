import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
INVENTORY = ROOT / "studio/docs/lane_baseline_injection_inventory.md"
ORCHESTRATOR = ROOT / "studio/orchestrator.py"
RUNNER = ROOT / "studio/runner.py"
PROVIDERS = ROOT / "studio/providers.py"
EXPERTS = ROOT / "studio/experts.py"

PYTHON_SOURCE_ANCHORS: dict[str, tuple[Path, str]] = {
    "class LaneContext": (ORCHESTRATOR, "LaneContext"),
    "async def _open_lane": (ORCHESTRATOR, "StudioSession._open_lane"),
    "async def _integrate_wave": (ORCHESTRATOR, "StudioSession._integrate_wave"),
    "StudioSession._run": (ORCHESTRATOR, "StudioSession._run"),
    "StudioSession._run_waves": (ORCHESTRATOR, "StudioSession._run_waves"),
    "StudioSession._open_lane": (ORCHESTRATOR, "StudioSession._open_lane"),
    "runner.git_worktree_add": (RUNNER, "git_worktree_add"),
    "StudioSession._lane_worktree_path": (ORCHESTRATOR, "StudioSession._lane_worktree_path"),
    "StudioSession._build_lane_experts": (ORCHESTRATOR, "StudioSession._build_lane_experts"),
    "experts._build_client": (EXPERTS, "_build_client"),
    "providers.CodexExpert._run_codex": (PROVIDERS, "CodexExpert._run_codex"),
    "providers._codex_env": (PROVIDERS, "_codex_env"),
    "providers.AntigravityExpert._run_antigravity": (
        PROVIDERS,
        "AntigravityExpert._run_antigravity",
    ),
    "StudioSession._integrate_wave": (ORCHESTRATOR, "StudioSession._integrate_wave"),
    "StudioSession._flush_lane_notes": (ORCHESTRATOR, "StudioSession._flush_lane_notes"),
    "runner.git_worktree_remove": (RUNNER, "git_worktree_remove"),
}


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _source_segment(path: Path, name: str) -> str:
    src = _read(path)
    tree = ast.parse(src)
    if "." in name:
        class_name, func_name = name.split(".", 1)
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and node.name == class_name:
                for child in node.body:
                    if (
                        isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef)
                        and child.name == func_name
                    ):
                        return ast.get_source_segment(src, child) or ""
    else:
        for node in tree.body:
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == name:
                return ast.get_source_segment(src, node) or ""
            if isinstance(node, ast.ClassDef) and node.name == name:
                return ast.get_source_segment(src, node) or ""
    raise AssertionError(f"找不到 source segment: {path}:{name}")


def _lane_context_fields() -> set[str]:
    tree = ast.parse(_read(ORCHESTRATOR))
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "LaneContext":
            return {
                child.target.id
                for child in node.body
                if isinstance(child, ast.AnnAssign) and isinstance(child.target, ast.Name)
            }
    raise AssertionError("找不到 LaneContext")


def _section_table_rows(text: str, heading: str) -> list[list[str]]:
    match = re.search(rf"^## {re.escape(heading)}\n(?P<body>.*?)(?=^## |\Z)", text, re.M | re.S)
    assert match, f"找不到章節: {heading}"
    rows: list[list[str]] = []
    for line in match.group("body").splitlines():
        if not line.startswith("|") or set(line.replace("|", "").strip()) <= {"-", ":"}:
            continue
        rows.append([cell.strip() for cell in line.strip("|").split("|")])
    return rows


def _inventory_python_source_anchors(text: str) -> set[str]:
    explicit_markers = set(re.findall(r"marker: `([^`]+)`", text))
    parenthesized_symbols = set(re.findall(r"（`([^`]+)`）", text))
    return explicit_markers | parenthesized_symbols


def test_inventory_declares_current_scope_and_lane_context_fields() -> None:
    text = _read(INVENTORY)
    fields = _lane_context_fields()

    assert "這份清單描述目前實作，不等同已落地的 lane baseline env/manifest 契約" in text
    assert "沒有 `lane manifest` 層" in text
    assert "顯式建構參數 + process-level config + env 繼承" in text
    for field in fields:
        assert f"`{field}`" in text, f"LaneContext 欄位未列入清單: {field}"


def test_inventory_source_markers_resolve_to_existing_symbols() -> None:
    text = _read(INVENTORY)
    anchors = _inventory_python_source_anchors(text)

    assert anchors == set(PYTHON_SOURCE_ANCHORS), f"文件程式錨點未同步: {sorted(anchors)}"
    for anchor, (path, symbol) in PYTHON_SOURCE_ANCHORS.items():
        assert _source_segment(path, symbol).strip(), f"程式錨點無法解析: {anchor}"


def test_inventory_table_covers_each_injected_item_without_manifest_claims() -> None:
    text = _read(INVENTORY)
    rows = _section_table_rows(text, "逐項對照")
    assert rows[0] == ["項目", "實際來源與欄位", "env 注入", "manifest 欄位", "缺失/失敗現況"]

    data = rows[1:]
    by_item = {row[0]: row for row in data}
    expected_items = {
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
    }
    assert expected_items <= by_item.keys()
    assert all(len(row) == 5 for row in data)
    assert all(row[3] == "無" for row in data), "現況清單不應宣稱已有 manifest 欄位"


def test_open_lane_and_worktree_startup_match_inventory() -> None:
    inventory = _read(INVENTORY)
    open_lane = _source_segment(ORCHESTRATOR, "StudioSession._open_lane")
    worktree_add = _source_segment(RUNNER, "git_worktree_add")

    assert 'branch = f"lane-{self.session_id}-"' in open_lane
    assert "wt = self._lane_worktree_path(branch)" in open_lane
    assert 'base = self._last_commit or "HEAD"' in open_lane
    assert "runner.git_worktree_add(self.cwd, wt, branch, base=base)" in open_lane
    assert "LaneContext(branch, wt, {}, branch=branch)" in open_lane
    assert "self._build_lane_experts(branch, wt)" in open_lane
    assert "env=" not in open_lane
    assert "manifest" not in open_lane.lower()

    assert '"git", "worktree", "add", "-b", branch, str(worktree_path), base' in worktree_add
    assert "sandbox=False" in worktree_add
    assert "env=" not in worktree_add

    for phrase in (
        "沒有 lane 專屬 env，也沒有寫入 manifest",
        "`git_worktree_add()` 回 False",
        "廣播「並行降級」後主幹重跑",
    ):
        assert phrase in inventory


def test_integrate_wave_has_no_env_or_manifest_injection_and_degrades() -> None:
    inventory = _read(INVENTORY)
    integrate_wave = _source_segment(ORCHESTRATOR, "StudioSession._integrate_wave")

    assert "sorted(lane_results, key=lambda x: x.ctx.lane_id)" in integrate_wave
    assert "await self._merge_lane(lr, plan_ctx)" in integrate_wave
    assert "self._flush_lane_notes(lr.ctx)" in integrate_wave
    assert "await self._teardown_lane(lr.ctx)" in integrate_wave
    assert "for task in deferred + crashed:" in integrate_wave
    assert "await self._run_task_in_lane(self._main_ctx, task, plan_ctx)" in integrate_wave
    assert "env" not in integrate_wave
    assert "manifest" not in integrate_wave.lower()

    assert "lane crash、worktree deferred、merge conflict 都降級到主幹序列化重跑" in inventory
    assert "crash lane 會清掉 notes" in inventory
    assert "沒有新增 env/manifest 注入" in inventory


def test_provider_env_inheritance_and_fail_examples_are_labeled_as_analogies() -> None:
    inventory = _read(INVENTORY)
    runner = _read(RUNNER)
    providers = _read(PROVIDERS)
    experts = _source_segment(EXPERTS, "_build_client")

    assert "run_env = {**os.environ, **env} if env is not None else None" in runner
    assert "env 為額外環境變數" in runner
    assert "預設 None＝繼承父行程環境" in runner
    assert "env=_codex_env()" in providers
    assert "env = os.environ.copy()" in providers
    assert 'env["CODEX_HOME"] = config.CODEX_HOME' in providers
    assert "env=os.environ.copy()" in providers
    assert "ClaudeAgentOptions(" in experts
    assert "cwd=str(cwd)" in experts
    assert "env=" not in experts

    assert "`CODEX_HOME` 是 provider 全域設定，非 per-lane baseline" in inventory
    assert "`AUTOPILOT_REPO`" in inventory
    assert "fail-closed 類比" in inventory
    assert "`TI_DISCUSS_MODE`" in inventory
    assert "fail-open 類比" in inventory
    assert "不是 lane baseline 現有欄位" in inventory
