"""專家慣例卡（studio/conventions.py）：執行環境慣例注入每位專家 system prompt。

治「慣例只寫在 CLAUDE.md 但專家沒被注入、每場重教且不遵守」（實證：同場混用
python/python3/.venv/bin/python 三種寫法、git status 64 次/場、同檔重讀 27 次）。

守護不變量：
- 卡內容分層（通用段恆在；Ti 段只在 cwd 是 ti-studio repo 時附加）、≤MAX_LINES 行。
- 注入走四個 Expert 類 __init__（涵蓋 make_expert 與 autopilot 直接建構路徑）。
- 無工具角色（oneshot 反思）不注入；冪等（重複 apply 不疊加）；原 Role 不變。
- 旋鈕 TI_CONVENTIONS_CARD=0 完全回舊行為。
- roles._COMMON 不受影響（role_store.builtin_body 往返依賴）。
"""

from __future__ import annotations

import dataclasses

import pytest

from studio import config, conventions, providers, role_store
from studio.roles import ENGINEER, PM


@pytest.fixture(autouse=True)
def _card_on(monkeypatch):
    monkeypatch.setattr(config, "CONVENTIONS_CARD", True)


@pytest.fixture
def ti_cwd(tmp_path):
    d = tmp_path / "ti"
    d.mkdir()
    (d / "pyproject.toml").write_text('[project]\nname = "ti-studio"\n', encoding="utf-8")
    return d


@pytest.fixture
def other_cwd(tmp_path):
    d = tmp_path / "other"
    d.mkdir()
    (d / "pyproject.toml").write_text('[project]\nname = "someapp"\n', encoding="utf-8")
    return d


# --- card 內容與守門 ---------------------------------------------------------


def test_card_generic_and_ti_sections(ti_cwd, other_cwd):
    ti = conventions.card(ti_cwd)
    generic = conventions.card(other_cwd)

    for key in (".venv/bin/python -m", "timeout 60", "git status", "$TMPDIR"):
        assert key in ti and key in generic, f"通用段須含 {key}"
    assert "pytest -q" in ti and "realgit" in ti and "0.14.4" in ti, "Ti 段速查"
    assert "realgit" not in generic, "外部專案不得帶 Ti 專屬段"


def test_card_line_budget(ti_cwd):
    n = len(conventions.card(ti_cwd).splitlines())
    assert (
        n <= conventions.MAX_LINES
    ), f"慣例卡 {n} 行超過 {conventions.MAX_LINES} 行上限——內容該搬去 skills/NOTES,不是養肥卡"


def test_card_knob_off_returns_empty(ti_cwd, monkeypatch):
    monkeypatch.setattr(config, "CONVENTIONS_CARD", False)
    assert conventions.card(ti_cwd) == ""


def test_is_ti_repo(ti_cwd, other_cwd, tmp_path):
    assert conventions._is_ti_repo(ti_cwd) is True
    assert conventions._is_ti_repo(other_cwd) is False
    assert conventions._is_ti_repo(tmp_path / "nonexistent") is False


# --- apply 語意 --------------------------------------------------------------


def test_apply_appends_and_keeps_original_frozen(ti_cwd):
    out = conventions.apply(ENGINEER, ti_cwd)
    assert "【執行慣例" in out.system_prompt
    assert out.system_prompt.startswith(ENGINEER.system_prompt), "卡附在尾端,角色身分文本不動"
    assert "【執行慣例" not in ENGINEER.system_prompt, "原 Role(frozen)不得被改"


def test_apply_skips_toolless_role(ti_cwd):
    oneshot = dataclasses.replace(PM, key="oneshot", allowed_tools=[], system_prompt="反思提示")
    out = conventions.apply(oneshot, ti_cwd)
    assert out is oneshot, "無工具角色(oneshot 反思)不得注入"


def test_apply_is_idempotent(ti_cwd):
    once = conventions.apply(ENGINEER, ti_cwd)
    twice = conventions.apply(once, ti_cwd)
    assert twice.system_prompt == once.system_prompt, "重複 apply 不得疊加"


# --- 四個 Expert 類接線 --------------------------------------------------------


def test_openai_expert_gets_card(ti_cwd):
    async def _chat(*a, **k):
        raise AssertionError("not called")

    ex = providers.OpenAIExpert(ENGINEER, "sid", ti_cwd, chat=_chat, model="m")
    assert "【執行慣例" in ex.role.system_prompt
    assert "【執行慣例" in ex._messages[0]["content"], "messages[0] 也要吃到加卡後的 prompt"


def test_codex_and_antigravity_get_card(ti_cwd):
    for cls in (providers.CodexExpert, providers.AntigravityExpert):
        ex = cls(ENGINEER, "sid", ti_cwd)
        assert "【執行慣例" in ex.role.system_prompt, cls.__name__


def test_claude_expert_gets_card_via_direct_construction(ti_cwd, monkeypatch):
    """涵蓋 autopilot 直接 Expert(...) 的路徑(調查分流/自評/拆分)——不經 make_expert。"""
    from studio import experts

    monkeypatch.setattr(experts.Expert, "_new_client", lambda self: object())
    ex = experts.Expert(ENGINEER, "sid", ti_cwd)
    assert "【執行慣例" in ex.role.system_prompt


def test_custom_role_via_role_store_gets_card(ti_cwd, tmp_path, monkeypatch):
    roles_dir = tmp_path / "roles"
    roles_dir.mkdir()
    (roles_dir / "auditor.md").write_text(
        "---\nname: 稽核員\navatar: 🔍\ntitle: Auditor\n---\n輸出格式:逐行 `重點:`。",
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "ROLES_DIR", str(roles_dir))
    try:
        errors = role_store.reload_roles()
        assert not errors, f"角色檔載入失敗:{errors}"
        from studio import roles as roles_mod

        role = roles_mod.BY_KEY["auditor"]
        ex = providers.CodexExpert(role, "sid", ti_cwd)
        assert "【執行慣例" in ex.role.system_prompt, "自訂角色同樣要吃到卡"
    finally:
        monkeypatch.undo()
        role_store.reload_roles()


def test_knob_off_no_injection_anywhere(ti_cwd, monkeypatch):
    monkeypatch.setattr(config, "CONVENTIONS_CARD", False)
    ex = providers.CodexExpert(ENGINEER, "sid", ti_cwd)
    assert "【執行慣例" not in ex.role.system_prompt


# --- _COMMON / role_store 往返不受影響 -----------------------------------------


def test_builtin_body_roundtrip_unaffected():
    body = role_store.builtin_body(ENGINEER)
    assert (
        body and "【執行慣例" not in body
    ), "卡不得進 Role 常數(builtin_body 往返依賴 _COMMON 原文)"
    assert not body.startswith("你是"), "removeprefix(_COMMON) 應已剝掉共通守則(往返未被破壞)"
