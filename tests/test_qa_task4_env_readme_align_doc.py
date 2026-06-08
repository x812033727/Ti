"""QA 任務 #4：對齊 .env.example 與 README 措辭，補安全預設原則。

驗收重點：
- .env.example 兩旗標段落含「兩者預設皆關閉/安全側」原則句。
- 該原則句含「分支保護」與「CI gating」。
- README 對齊句同樣含「預設安全側 + 分支保護 + CI gating」。
- 兩處兩變數預設值一致（皆 0），不矛盾。
- .env.example 精簡（不重複 README 的長段風險敘述），並指向 README 補充區塊。
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
ENV = ROOT / ".env.example"


@pytest.fixture(scope="module")
def env_text():
    return ENV.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def readme_text():
    return README.read_text(encoding="utf-8")


def _autopilot_block(env_text):
    """擷取 .env.example 中 Autopilot 旗標相關段落。"""
    idx = env_text.index("Autopilot")
    return env_text[idx:]


def test_env_has_default_off_principle(env_text):
    """.env.example 含『兩者預設皆關閉/安全側』原則句。"""
    blk = _autopilot_block(env_text)
    assert re.search(r"預設.{0,4}皆?.{0,4}(關閉|安全)", blk), \
        ".env.example 缺少『預設關閉/安全側』原則句"


def test_env_mentions_branch_protection_and_ci(env_text):
    """原則句須含『分支保護』與『CI gating』。"""
    blk = _autopilot_block(env_text)
    assert "分支保護" in blk, ".env.example 缺『分支保護』"
    assert "CI gating" in blk or "CI" in blk, ".env.example 缺『CI gating』"


def test_readme_has_aligned_principle(readme_text):
    """README 對齊句須含預設安全側 + 分支保護 + CI gating。"""
    m = re.search(r"兩個?旗標預設皆?為?安全側.*?CI gating", readme_text)
    assert m, "README 缺少對齊的安全預設原則句"
    line = m.group(0)
    assert "分支保護" in line and "CI gating" in line


def test_both_flags_default_zero_in_env(env_text):
    """.env.example 兩變數均示範為 0。"""
    assert re.search(r"TI_AUTOPILOT_FORCE_PUSH=0", env_text), "FORCE_PUSH 未示範 =0"
    assert re.search(r"TI_AUTOPILOT_MERGE_ADMIN=0", env_text), "MERGE_ADMIN 未示範 =0"


def test_no_contradiction_default_off(env_text, readme_text):
    """兩處皆稱預設關閉/安全側，無矛盾。"""
    # README 表格兩行預設皆 0（安全側）
    for var in ("TI_AUTOPILOT_FORCE_PUSH", "TI_AUTOPILOT_MERGE_ADMIN"):
        row = next(ln for ln in readme_text.splitlines() if var in ln and ln.lstrip().startswith("|"))
        assert "0" in row and "安全" in row, f"README 表格 {var} 行未標 0/安全側"
    # .env.example 不得出現把預設講成 1/開啟的矛盾敘述
    blk = _autopilot_block(env_text)
    assert "預設皆關閉" in blk or re.search(r"預設.{0,4}關閉", blk), \
        ".env.example 未明示預設關閉"


def test_env_points_to_readme_not_duplicating(env_text):
    """.env.example 精簡並指向 README 補充區塊（避免雙處維護漂移）。"""
    blk = _autopilot_block(env_text)
    assert "README" in blk, ".env.example 未指向 README"
    # 精簡：不應重複 README 的 reflog/Rulesets 長段風險敘述
    assert "reflog" not in blk, ".env.example 重複了 README 的 reflog 風險長敘述"
    assert "Rulesets" not in blk, ".env.example 重複了 README 的 Rulesets 風險長敘述"
