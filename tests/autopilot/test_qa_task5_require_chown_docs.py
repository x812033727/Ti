"""QA 任務 #5：驗證 README.md 與 .env.example 的 TI_REQUIRE_CHOWN 文件斷言。

對應驗收標準 #7：
  README 與 .env.example 含 TI_REQUIRE_CHOWN、strict、Breaking change/breaking、warn、root。

破壞性思考：不只驗「字串存在」，還要驗語意脈絡正確——
  - strict 必須被標為「預設」
  - 必須出現 warn 過渡語意
  - 必須出現 root 字樣（owner 驗證對象）
  - breaking change 必須被明確標示
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
README = ROOT / "README.md"
ENV = ROOT / ".env.example"


def _read(p: Path) -> str:
    assert p.exists(), f"檔案不存在：{p}"
    return p.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def readme() -> str:
    return _read(README)


@pytest.fixture(scope="module")
def env() -> str:
    return _read(ENV)


# ---- 基本字串斷言（驗收標準 #7 逐項）----


@pytest.mark.parametrize("token", ["TI_REQUIRE_CHOWN", "strict", "warn", "root"])
def test_readme_contains_token(readme, token):
    assert token in readme, f"README 缺少必要字樣：{token!r}"


@pytest.mark.parametrize("token", ["TI_REQUIRE_CHOWN", "strict", "warn", "root"])
def test_env_contains_token(env, token):
    assert token in env, f".env.example 缺少必要字樣：{token!r}"


def test_readme_marks_breaking_change(readme):
    assert re.search(r"[Bb]reaking change", readme), "README 未標示 Breaking change"


def test_env_marks_breaking_change(env):
    assert re.search(r"[Bb]reaking change", env), ".env.example 未標示 Breaking change"


# ---- 語意脈絡斷言（破壞性：防「字串湊齊但語意錯」）----


def test_readme_strict_is_default(readme):
    """strict 必須在語意上被標為預設值，而非只是被提及。"""
    # 同一段落/同一行內 strict 與「預設」共現
    assert re.search(r"strict[^\n]{0,40}預設", readme) or re.search(
        r"預設[^\n]{0,40}strict", readme
    ), "README 未把 strict 標示為預設值"


def test_env_strict_is_default(env):
    assert re.search(r"strict[^\n]{0,40}預設", env) or re.search(r"預設[^\n]{0,40}strict", env), (
        ".env.example 未把 strict 標示為預設值"
    )


def test_readme_has_warn_transition_meaning(readme):
    """warn 必須描述為過渡/放行語意，而非孤立出現。"""
    assert re.search(r"warn[^\n]{0,60}(過渡|放行|warning)", readme) or re.search(
        r"(過渡|放行)[^\n]{0,60}warn", readme
    ), "README 未描述 warn 的過渡/放行語意"


def test_env_has_warn_transition_meaning(env):
    assert re.search(r"warn[^\n]{0,60}(過渡|放行|warning)", env) or re.search(
        r"(過渡|放行)[^\n]{0,60}warn", env
    ), ".env.example 未描述 warn 的過渡語意"


def test_readme_root_in_chown_context(readme):
    """root 字樣必須與 owner/uid/寫入語意相關，不是文中其他 root 巧合。"""
    assert re.search(r"root[^\n]{0,30}(owner|uid|0|寫入|安全)", readme) or re.search(
        r"(owner|uid|寫入|安全)[^\n]{0,30}root", readme
    ), "README 的 root 字樣未落在安全寫入脈絡"


# ---- off 三態完整性（設計定案三態，文件不應只講兩態）----


def test_readme_documents_off_mode(readme):
    assert "off" in readme, "README 未說明 off 模式"


def test_env_documents_off_mode(env):
    assert "off" in env, ".env.example 未說明 off 模式"
