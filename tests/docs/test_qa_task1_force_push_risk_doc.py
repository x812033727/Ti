"""QA 任務 #1：README 為 TI_AUTOPILOT_FORCE_PUSH 補上風險提示。

驗收重點：
- 風險敘述出現在「表格下方」的補充區塊（表格行為檔案中首個含完整變數名之行）。
- 明確點出「誤用會覆蓋/覆寫他人 commit」。
- 明確點出「reflog 救援僅本機」性質。
- 不破壞 test_qa_task6_docs 的取行邏輯（首個完整變數名行為 KV 表格行、同行含 0 與安全側）。
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


def test_force_push_table_row_is_first_mention_and_safe_default(lines):
    """首個含 FORCE_PUSH 完整名稱的行＝KV 表格行，同行含 `0` 與安全側。"""
    idx, row = _first_line_with(lines, "TI_AUTOPILOT_FORCE_PUSH")
    assert idx != -1, "README 找不到 TI_AUTOPILOT_FORCE_PUSH"
    assert row.lstrip().startswith("|"), f"首次出現非表格行：{row!r}"
    assert "0" in row, "表格行未標明預設值 0"
    assert "安全" in row, "表格行未標明安全側"


def test_force_push_has_overwrite_others_commit_risk(text):
    """風險提示：誤用會覆蓋/覆寫他人 commit。"""
    assert re.search(r"(覆蓋|覆寫).{0,8}他人.{0,4}commit", text), (
        "缺少『覆蓋/覆寫他人 commit』風險敘述"
    )


def test_force_push_has_reflog_local_only_note(text):
    """風險提示：救援僅靠本機 reflog。"""
    assert "reflog" in text, "缺少 reflog 字樣"
    # reflog 同段需點明本機性
    assert re.search(r"本機.{0,6}reflog|reflog[^\n]{0,30}本機", text), (
        "缺少『reflog 僅本機』的本機性敘述"
    )


def test_force_push_risk_below_table(lines):
    """風險敘述出現在表格行之後（補充區塊在下方）。"""
    row_idx, _ = _first_line_with(lines, "TI_AUTOPILOT_FORCE_PUSH")
    risk_idx, _ = _first_line_with(lines, "reflog")
    assert risk_idx > row_idx, "reflog 風險敘述未置於表格行下方"


def test_uses_force_with_lease_not_bare_force(text):
    """文件描述與程式一致：用 --force-with-lease，絕不裸 -f。"""
    assert "--force-with-lease" in text
    assert "--force-if-includes" in text
