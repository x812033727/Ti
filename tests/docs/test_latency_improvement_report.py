"""QA 守門：延遲改善對比報告必須拆清離線證據與真環境待補驗。"""

from __future__ import annotations

import re
from pathlib import Path

from studio import roles

ROOT = Path(__file__).resolve().parents[2]
REPORT = ROOT / "docs" / "latency-improvement-report.md"
ROLES_PY = ROOT / "studio" / "roles.py"
ROLE_TEST = ROOT / "tests" / "core" / "test_roles.py"

OUTPUT_LIMIT_FRAGMENTS = (
    "單則發言的自由散文部分限 500 字內",
    "結構化標記行",
    "`任務:`/`驗證:`/`決議:`",
    "`依賴:`/`後續任務:`/`核心改動:`",
    "必要條列內容不計入",
)


def _report() -> str:
    assert REPORT.exists(), "缺 docs/latency-improvement-report.md"
    return REPORT.read_text(encoding="utf-8")


def _section(text: str, heading: str) -> str:
    start = text.index(heading)
    next_heading = text.find("\n## ", start + len(heading))
    return text[start:] if next_heading == -1 else text[start:next_heading]


def test_improvement_report_splits_offline_and_real_environment_layers() -> None:
    text = _report()

    assert "離線層" in text, "報告須明確拆出離線層"
    assert "真環境層" in text, "報告須明確拆出真環境層"
    assert "指示注入 diff" in text
    assert "8 角色覆蓋證據" in text

    real_env = _section(text, "## 四、真環境層")
    assert "N/A" in real_env
    assert "未打真 API" in real_env
    assert "輸出" in real_env and "待補驗" in text
    assert "軟性 prompt 指示" in text
    assert "不是 provider `max_tokens` 硬上限" in text


def test_improvement_report_offline_evidence_matches_role_prompt_and_guard() -> None:
    text = _report()
    roles_source = ROLES_PY.read_text(encoding="utf-8")
    role_test_source = ROLE_TEST.read_text(encoding="utf-8")

    assert len(roles.BUILTIN_ROLES) == 8
    assert "roles.BUILTIN_ROLES" in text
    assert "builtin_roles: 8" in text
    assert "role_keys: pm,engineer,qa,senior,researcher,architect,security,devops" in text
    assert "missing: {}" in text

    for fragment in OUTPUT_LIMIT_FRAGMENTS:
        assert fragment in text, f"報告缺離線證據片段：{fragment!r}"
        assert fragment in roles_source, f"roles.py 未注入：{fragment!r}"
        assert fragment in role_test_source, f"守門測試未覆蓋：{fragment!r}"
        for role in roles.BUILTIN_ROLES:
            assert fragment in role.system_prompt, f"{role.key} prompt 未含：{fragment!r}"


def test_improvement_report_real_revalidation_commands_are_concrete() -> None:
    text = _report()

    assert "TI_OFFLINE=0" in text, "補驗必須明確禁止離線模式"
    assert "TI_PROVIDER=claude" in text, "補驗須給出真 provider 範例"
    assert "TestClient" in text and 'websocket_connect("/ws")' in text
    assert "history/*.meta.json" in text
    assert "latency.total:" in text
    assert "token_usage.total:" in text
    assert "latency.by_role" in text
    assert "token_usage.by_role" in text
    assert "completion" in text and "avg_completion_per_call" in text


def test_improvement_report_disqualifies_zero_or_fake_provider_evidence() -> None:
    text = _report()

    disqualifiers = (
        "`token_usage.total.calls == 0`",
        "`latency.total.count == 0`",
        "`token_usage.by_provider` 只有 `fake`",
        "`latency.by_role` 或 `token_usage.by_role` 為空",
    )
    for disqualifier in disqualifiers:
        assert disqualifier in text, f"報告缺不合格條件：{disqualifier}"

    fake_line = next(line for line in text.splitlines() if "只有 `fake`" in line)
    assert "不合格" in _section(text, "## 四、真環境層") or "不可" in fake_line


def test_improvement_report_commands_use_venv_python_not_bare_pytest() -> None:
    text = _report()

    assert "timeout 300 .venv/bin/python -m pytest -q" in text
    assert "timeout 60 .venv/bin/python -c" in text
    assert "timeout 60 .venv/bin/python - <<'PY'" in text
    assert re.search(r"(?<!-m )(?<![\w/.-])pytest(?![\w.-])", text) is None
    assert re.search(r"(?<![\w/.-])python(?![3\w])", text) is None
