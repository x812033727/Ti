"""QA 任務 #3：README 變數表旁加註解析規則。

驗收重點：
- README 出現「解析規則」說明，且置於表格下方補充區塊。
- 明確列舉關閉集合：0 / false / False / 空值 / 未設，其餘皆視為開啟。
- 點明陷阱：FALSE（全大寫）/ no / off 等不在關閉集合內，會被當成開啟。
- 明說區分大小寫、無 .lower()，與 config.py 字面一致（不撒謊）。
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
CONFIG = ROOT / "studio" / "config.py"


@pytest.fixture(scope="module")
def lines():
    return README.read_text(encoding="utf-8").splitlines()


@pytest.fixture(scope="module")
def text(lines):
    return "\n".join(lines)


RULE_MARK = "**解析規則**"  # 補充區塊真正的解析規則段（避開 L120 HTML 維護註解）


def test_parse_rule_section_exists(text):
    assert RULE_MARK in text, "README 補充區塊缺少『**解析規則**』說明段"


def test_parse_rule_lists_off_set(text):
    """關閉集合需列出 0 / false / False / 空值/未設。"""
    seg = text[text.index(RULE_MARK) :]
    for token in ("0", "false", "False"):
        assert token in seg, f"解析規則未列出關閉值 {token!r}"
    assert "空值" in seg or "空字串" in seg, "解析規則未提及空值"
    assert "未設" in seg, "解析規則未提及未設定"


def test_parse_rule_else_is_true(text):
    """非關閉集合一律視為開啟。"""
    seg = text[text.index(RULE_MARK) :]
    assert re.search(r"其餘.{0,8}(視為|當成|判為|皆).{0,4}開啟|皆.{0,4}開啟", seg), (
        "解析規則未說明『其餘皆視為開啟』"
    )


def test_parse_rule_warns_case_sensitive_traps(text):
    """陷阱：FALSE/no/off 等不在關閉集合，會被當開啟；且區分大小寫。"""
    seg = text[text.index(RULE_MARK) :]
    assert "FALSE" in seg, "未點出 FALSE（全大寫）陷阱"
    assert "no" in seg or "off" in seg, "未點出 no/off 等誤填陷阱"
    assert re.search(r"區分大小寫|大小寫敏感|無\s*\.?lower", seg), "未說明區分大小寫（無 .lower()）"


def test_doc_matches_config_literal(text):
    """文件描述的關閉集合須與 config.py 字面一致（不撒謊）。"""
    cfg = CONFIG.read_text(encoding="utf-8")
    # 取 FORCE_PUSH 該行的字面集合
    m = re.search(r"TI_AUTOPILOT_FORCE_PUSH.*?not in \(([^)]*)\)", cfg, re.S)
    assert m, "config.py 找不到 FORCE_PUSH 的 not in (...) 字面"
    literals = re.findall(r'"([^"]*)"', m.group(1))
    assert literals == ["0", "false", "False", ""], f"config 關閉集合與預期不符：{literals}"
    # 確認 config 無 .lower() 套在該變數（文件聲稱區分大小寫）
    assert ".lower()" not in m.group(0), "config FORCE_PUSH 竟有 .lower()，文件區分大小寫敘述會失準"
    # 文件須照同樣字面列舉，且不可暗示大小寫不敏感
    assert "not in" in text and '("0", "false", "False", "")'.replace(" ", "") in text.replace(
        " ", ""
    ), "README 未照 config 字面列出判斷式"


def test_parse_rule_below_table(lines):
    table_idx = next(i for i, ln in enumerate(lines) if "TI_AUTOPILOT_FORCE_PUSH" in ln)
    rule_idx = next(i for i, ln in enumerate(lines) if RULE_MARK in ln)
    assert rule_idx > table_idx, "解析規則未置於表格下方"
