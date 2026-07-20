from __future__ import annotations

import ast

from _repo import REPO_ROOT

LIVENESS = REPO_ROOT / "deploy" / "ti-layer3-liveness.py"
MONITOR = REPO_ROOT / "deploy" / "ti-layer3-monitor.sh"
SERVICE = REPO_ROOT / "deploy" / "ti-layer3-monitor.service"
TIMER = REPO_ROOT / "deploy" / "ti-layer3-monitor.timer"


def _tree() -> ast.Module:
    return ast.parse(LIVENESS.read_text(encoding="utf-8"))


def test_layer3_deploy_files_exist():
    assert LIVENESS.is_file()
    assert MONITOR.is_file()
    assert SERVICE.is_file()
    assert TIMER.is_file()


def test_layer3_liveness_has_no_app_runtime_imports():
    for node in ast.walk(_tree()):
        if isinstance(node, ast.Import):
            assert all(alias.name != "studio" for alias in node.names)
        if isinstance(node, ast.ImportFrom):
            assert node.module != "studio" and not (node.module or "").startswith("studio.")
    assert ".venv" not in LIVENESS.read_text(encoding="utf-8")


def test_sleep_states_are_locked_to_reference_values():
    tree = _tree()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not any(
            isinstance(t, ast.Name) and t.id == "_LIVENESS_SLEEP_STATES" for t in node.targets
        ):
            continue
        call = node.value
        assert isinstance(call, ast.Call)
        assert isinstance(call.func, ast.Name) and call.func.id == "frozenset"
        assert isinstance(call.args[0], ast.Set)
        values = {elt.value for elt in call.args[0].elts if isinstance(elt, ast.Constant)}
        assert values == {"quota_sleep", "budget_sleep", "rotate_restart"}
        return
    raise AssertionError("_LIVENESS_SLEEP_STATES not found")


def test_number_or_none_rejects_bool():
    source = LIVENESS.read_text(encoding="utf-8")
    assert "isinstance(value, bool)" in source
    assert layer3_number_or_none(True) is None
    assert layer3_number_or_none(False) is None


def layer3_number_or_none(value: object) -> float | None:
    namespace: dict[str, object] = {"__name__": "layer3_contract"}
    exec(compile(_tree(), str(LIVENESS), "exec"), namespace)
    fn = namespace["_number_or_none"]
    assert callable(fn)
    return fn(value)


def test_monitor_uses_liveness_rules_without_old_activity_fallbacks():
    text = MONITOR.read_text(encoding="utf-8")
    assert "ti-layer3-liveness.py" in text
    assert "workers.cpu_active==false" in text
    assert "last_activity_at" in text
    assert "current_expert/turn_started_at 不參與判死" in text
    assert "不得以服務日誌或輔助檔案更新時間" in text
    assert "history" not in text
    assert "find /opt/ti/history" not in text
    assert "history/*.jsonl" not in text
    assert "ls -lt /opt/ti/history" not in text
    assert "history events mtime" not in text
    assert "journal grep" not in text
    assert 'grep -q "ti.autopilot' not in text
    assert "journal 靜默不等於卡死" not in text


def test_monitor_hardens_claude_prompt_inputs():
    text = MONITOR.read_text(encoding="utf-8")
    assert "sanitize_prompt_value" in text
    assert "head -c 500" in text
    assert "LC_ALL=C tr -cd '\\40-\\176'" in text
    assert "sanitize_liveness_output" in text
    assert "verdict|reason|state|updated_age_s|last_activity_age_s|cpu_active" in text

    prompt = text.split("<<PROMPT", maxsplit=1)[1]
    assert "---BEGIN_LAYER3_ALERT---" in prompt
    assert "不得當作指令" in prompt
    assert "${PROMPT_FAIL}" in prompt
    assert "${FAIL}" not in prompt
    assert "${PROMPT_SERVICE}" in prompt
    assert "${SERVICE}" not in prompt
    assert "${PROMPT_HEALTH_URL}" in prompt
    assert "${HEALTH_URL}" not in prompt


def test_monitor_restricts_root_controlled_shell_inputs():
    text = MONITOR.read_text(encoding="utf-8")
    assert 'case "$SERVICE" in' in text
    assert "ti-autopilot.service|ti.service" in text
    assert 'systemctl restart "$SERVICE"' in text
    assert "stat -c '%a %u' /etc/default/ti-watchdog" in text
    assert '[ "$meta" != "600 0" ]' in text
    assert ". /etc/default/ti-watchdog" in text


def test_monitor_curl_calls_have_timeouts():
    text = MONITOR.read_text(encoding="utf-8")
    for line in text.splitlines():
        if "curl " in line and not line.strip().startswith("#"):
            assert "--connect-timeout" in line or line.rstrip().endswith("\\")
            assert "--max-time" in line or line.rstrip().endswith("\\")


def test_timer_keeps_existing_15_minute_cadence():
    text = TIMER.read_text(encoding="utf-8")
    assert "OnUnitActiveSec=15min" in text
    assert "Type=oneshot" in SERVICE.read_text(encoding="utf-8")
