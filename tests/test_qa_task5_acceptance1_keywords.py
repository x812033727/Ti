"""QA 任務 #5：守住驗收標準 1 的字面要求。

驗收標準 1 原文：test_qa_task6_docs.py 全數通過（兩變數名、預設值 0、安全側
字樣、--force-with-lease / --force-if-includes / --admin / 分支保護 等關鍵字
皆存在）。本測試把這些關鍵字逐一釘死，並守住 test_qa_task6_docs 賴以運作的
next() 取行不變量（README 首個含變數名的行必須是表格行，同行含 0/安全側）。
"""
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
README = (ROOT / "README.md").read_text(encoding="utf-8")
CONFIG = (ROOT / "studio" / "config.py").read_text(encoding="utf-8")
AUTOPILOT = (ROOT / "studio" / "autopilot.py").read_text(encoding="utf-8")
ALL_DOCS = README + CONFIG + AUTOPILOT


@pytest.mark.parametrize("var", ["TI_AUTOPILOT_FORCE_PUSH", "TI_AUTOPILOT_MERGE_ADMIN"])
def test_both_variable_names_present(var):
    assert var in README, f"README 缺變數名 {var}"


@pytest.mark.parametrize("kw", ["--force-with-lease", "--force-if-includes", "--admin", "分支保護"])
def test_required_keywords_present(kw):
    assert kw in ALL_DOCS, f"文件群缺關鍵字 {kw}"


def test_default_zero_and_safe_wording_present():
    assert "0（安全側）" in README, "README 缺『0（安全側）』字樣"


@pytest.mark.parametrize("var", ["TI_AUTOPILOT_FORCE_PUSH", "TI_AUTOPILOT_MERGE_ADMIN"])
def test_first_mention_is_table_row(var):
    """守住 next() 取行不變量：首個含變數名之行＝KV 表格行，同行有 0 與安全/預設。"""
    line = next(ln for ln in README.splitlines() if var in ln)
    assert line.lstrip().startswith("|"), f"{var} 首次出現非表格行：{line!r}"
    assert "0" in line and ("安全" in line or "預設" in line), \
        f"{var} 表格行未標明預設值：{line!r}"
