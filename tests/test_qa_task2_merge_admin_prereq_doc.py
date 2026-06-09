"""QA 任務 #2：README 為 TI_AUTOPILOT_MERGE_ADMIN 補上前提條件。

驗收重點：
- 前提敘述置於表格下方補充區塊（表格行為檔案中首個含完整變數名之行）。
- 明確點出「需 repo admin 權限」。
- 明確點出「Rulesets 下 --admin 可能不生效」。
- 不寫成程式未實作的保證（不暗示工具自動偵測權限/Rulesets）。
- 不破壞 test_qa_task6_docs 取行邏輯（首個完整變數名行＝KV 表格行、同行含 0 與安全側）。
"""

import re

import pytest
from _repo import REPO_ROOT

README = REPO_ROOT / "README.md"


@pytest.fixture(scope="module")
def lines():
    return README.read_text(encoding="utf-8").splitlines()


@pytest.fixture(scope="module")
def text(lines):
    return "\n".join(lines)


def _first_line_with(lines, token):
    for i, ln in enumerate(lines):
        if token in ln:
            return i, ln
    return -1, ""


def test_merge_admin_table_row_is_first_mention_and_safe_default(lines):
    idx, row = _first_line_with(lines, "TI_AUTOPILOT_MERGE_ADMIN")
    assert idx != -1, "README 找不到 TI_AUTOPILOT_MERGE_ADMIN"
    assert row.lstrip().startswith("|"), f"首次出現非表格行：{row!r}"
    assert "0" in row, "表格行未標明預設值 0"
    assert "安全" in row, "表格行未標明安全側"


def test_merge_admin_requires_admin_permission(text):
    """前提：需 repo admin 權限。"""
    assert re.search(r"admin\s*權限", text), "缺少『admin 權限』前提敘述"


def test_merge_admin_rulesets_limitation(text):
    """限制：Rulesets 下 --admin 可能不生效。"""
    assert "Rulesets" in text, "缺少 Rulesets 字樣"
    # Rulesets 與 --admin 失效需在同段點明
    assert re.search(
        r"--admin.{0,40}(無法繞過|不生效|被擋|失效)|Rulesets.{0,60}(無法繞過|不生效|被擋|失效)",
        text,
    ), "缺少『Rulesets 下 --admin 可能不生效』的限制敘述"


def test_merge_admin_prereq_below_table(lines):
    """前提敘述置於表格行下方。"""
    row_idx, _ = _first_line_with(lines, "TI_AUTOPILOT_MERGE_ADMIN")
    rs_idx, _ = _first_line_with(lines, "Rulesets")
    assert rs_idx > row_idx, "Rulesets 限制敘述未置於表格行下方"


def test_no_overpromise(text):
    """不得寫成未實作的保證：設 1 不代表保證能自動合併。"""
    # 文件須明說「不代表保證」之類語意，避免使用者誤解
    assert re.search(r"不(代表|保證|等於).{0,12}(保證|自動合併|一定)", text), (
        "缺少『設 1 不保證自動合併』的去除過度承諾敘述"
    )


def test_uses_gh_pr_merge_admin(text):
    """文件描述與程式一致：使用 gh pr merge --admin。"""
    assert "gh pr merge --admin" in text
