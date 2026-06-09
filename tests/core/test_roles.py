"""新增團隊角色與資安決議解析的單元測試。"""

from __future__ import annotations

from studio import config, roles
from studio.orchestrator import security_approved


def test_core_roles_unchanged():
    assert [r.key for r in roles.CORE_ROLES] == ["pm", "engineer", "qa", "senior"]


def test_new_roles_registered():
    for key in ("researcher", "architect", "security", "devops"):
        assert key in roles.BY_KEY


def test_roster_is_core_plus_enabled_optional():
    expected = roles.CORE_ROLES + [
        r for r in roles._OPTIONAL_ROLES if r.key in config.OPTIONAL_ROLES
    ]
    assert roles.ROSTER == expected


def test_researcher_has_web_tools():
    r = roles.BY_KEY["researcher"]
    assert "WebSearch" in r.allowed_tools and "WebFetch" in r.allowed_tools


def test_security_can_run_static_checks_but_not_write():
    r = roles.BY_KEY["security"]
    assert "Bash" in r.allowed_tools
    assert "Write" not in r.allowed_tools and "Edit" not in r.allowed_tools


def test_architect_is_read_only():
    r = roles.BY_KEY["architect"]
    assert "Write" not in r.allowed_tools and "Bash" not in r.allowed_tools


def test_security_approved_parses_verdict():
    assert security_approved("看起來沒問題。\n決議: 安全核可") is True
    assert security_approved("有路徑穿越風險。\n決議: 安全退回") is False


def test_security_approved_fallback():
    # 沒有明確標記時，看是否出現風險字樣
    assert security_approved("一切正常，無明顯問題") is True
    assert security_approved("發現 SQL injection 漏洞") is False
