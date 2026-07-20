"""設定面板補旋鈕(功能強化 C3):Autopilot 組+進階組新欄、textarea kind、數值防線。

守護不變量:
- 12 個新 env 全在 ALLOWED 白名單且分組正確。
- textarea kind:read() 曝露、update() 把多行摺成單行(.env 單行格式)。
- numeric 欄:非法值(int 不可解析)不落檔——防 config.reload() 的 int() 炸掉整個行程。
- select 非法值照舊被拒。
"""

from __future__ import annotations

import pytest

from studio import settings

NEW_AUTOPILOT = {
    "TI_AUTOPILOT_NORTH_STAR",
    "TI_AUTOPILOT_AUTO_MERGE",
    "TI_AUTOPILOT_WORKFLOW_TRIAGE",
    "TI_AUTOPILOT_INVESTIGATION_LANE",
    "TI_AUTOPILOT_INVESTIGATION_REFUTE",
    "TI_AUTOPILOT_INVESTIGATION_TIMEOUT",
    "TI_AUTOPILOT_FOLLOWUP_MAX_PER_TASK",
    "TI_AUTOPILOT_FOLLOWUP_MAX_GEN",
}
NEW_ADVANCED = {
    "TI_LINT_AUTOFORMAT",
    "TI_EXPERT_LINT_HOOK",
    "TI_CONVENTIONS_CARD",
    "TI_EXPERT_SKILLS",
}


def test_new_envs_in_allowed_and_grouped():
    by_env = {f.env: f for f in settings.FIELDS}
    for env in NEW_AUTOPILOT:
        assert env in settings.ALLOWED, env
        assert by_env[env].group == "Autopilot", env
    for env in NEW_ADVANCED:
        assert env in settings.ALLOWED, env
        assert by_env[env].group == "進階", env


def test_north_star_is_textarea():
    f = next(f for f in settings.FIELDS if f.env == "TI_AUTOPILOT_NORTH_STAR")
    assert f.kind == "textarea"


@pytest.fixture
def env_file(tmp_path, monkeypatch):
    p = tmp_path / ".env"
    monkeypatch.setattr(settings, "env_path", lambda: str(p))
    return p


def test_numeric_guard_rejects_garbage(env_file, monkeypatch):
    settings.update({"TI_AUTOPILOT_INVESTIGATION_TIMEOUT": "abc"})
    text = env_file.read_text() if env_file.exists() else ""
    assert "TI_AUTOPILOT_INVESTIGATION_TIMEOUT" not in text, "非法數值不得落檔"

    settings.update({"TI_AUTOPILOT_INVESTIGATION_TIMEOUT": "900"})
    assert "TI_AUTOPILOT_INVESTIGATION_TIMEOUT" in env_file.read_text(), "合法數值照常落檔"


def test_textarea_folds_newlines(env_file):
    settings.update({"TI_AUTOPILOT_NORTH_STAR": "第一行\n第二行\n  第三行"})
    text = env_file.read_text()
    line = next(ln for ln in text.splitlines() if ln.startswith("TI_AUTOPILOT_NORTH_STAR"))
    assert "\\n" not in line and "第一行 第二行 第三行" in line, ".env 單行格式:多行摺成空白"


def test_select_still_rejects_invalid(env_file):
    settings.update({"TI_AUTOPILOT_AUTO_MERGE": "maybe"})
    text = env_file.read_text() if env_file.exists() else ""
    assert "TI_AUTOPILOT_AUTO_MERGE" not in text
