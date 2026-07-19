"""任務 #1 驗收：「執行環境前置」段開頭新增「前置條件 checklist」。

對應 PM 驗收標準 1：段開頭有 checklist，依「依賴／secrets／token」三類，
明確涵蓋 Python ≥ 3.11、ANTHROPIC_API_KEY（必備）、GITHUB_TOKEN 與登入密碼（標選填）。

並守住設計紅線：checklist 不得寫出 TI_AUTOPILOT_* 完整變數名（首現須留在「設定」表）。
"""

import re

from _repo import REPO_ROOT

ROOT = REPO_ROOT
README = (ROOT / "README.md").read_text(encoding="utf-8")


def _section(title: str) -> str:
    m = re.search(
        rf"^##\s+{re.escape(title)}\s*$(.*?)(?=^##\s|\Z)", README, re.MULTILINE | re.DOTALL
    )
    assert m, f"找不到段落: ## {title}"
    return m.group(1)


SEC = _section("執行環境前置")


def _checklist_block() -> str:
    """擷取『前置條件 checklist』到第一個 ### 子標題之前的開頭區塊。"""
    m = re.search(r"前置條件\s*checklist(.*?)(?=^###\s)", SEC, re.MULTILINE | re.DOTALL)
    assert m, "『執行環境前置』段開頭找不到『前置條件 checklist』區塊"
    return m.group(1)


# ---- checklist 存在且位於段開頭（在第 1. 建立虛擬環境之前） ----
def test_checklist_present():
    assert "前置條件" in SEC and "checklist" in SEC, "缺『前置條件 checklist』"


def test_checklist_at_section_top():
    idx_checklist = SEC.find("前置條件")
    idx_step1 = SEC.find("### 1.")
    assert idx_checklist != -1 and idx_step1 != -1
    assert idx_checklist < idx_step1, "checklist 必須在『### 1. 建立虛擬環境』之前（段開頭）"


# ---- 三類分類：依賴／secrets／token ----
def test_checklist_three_categories():
    block = _checklist_block()
    assert "依賴" in block, "checklist 缺『依賴』類"
    assert "secrets" in block.lower(), "checklist 缺『secrets』類"
    assert "token" in block.lower(), "checklist 缺『token』類"


# ---- 依賴：Python ≥ 3.11 ----
def test_checklist_python_version():
    block = _checklist_block()
    assert "3.11" in block, "checklist 未列 Python ≥ 3.11"
    assert "Python" in block


# ---- secrets：ANTHROPIC_API_KEY 必備 ----
def test_checklist_anthropic_key_required():
    block = _checklist_block()
    assert "ANTHROPIC_API_KEY" in block, "checklist 缺 ANTHROPIC_API_KEY"
    # ANTHROPIC_API_KEY 所在行須標明『必備』
    line = next(ln for ln in block.splitlines() if "ANTHROPIC_API_KEY" in ln)
    assert "必備" in line, f"ANTHROPIC_API_KEY 未標『必備』：{line}"


# ---- token／選填：GITHUB_TOKEN 與登入密碼（標選填） ----
def test_checklist_github_token_optional():
    block = _checklist_block()
    assert "GITHUB_TOKEN" in block, "checklist 缺 GITHUB_TOKEN"


def test_checklist_login_password_optional():
    block = _checklist_block()
    assert "密碼" in block, "checklist 缺『登入密碼』"


def test_checklist_marks_optional():
    """GITHUB_TOKEN／登入密碼 該類須明確標『選填』。"""
    block = _checklist_block()
    assert "選填" in block, "checklist 的 token 類未標『選填』"


# ---- 紅線：checklist 不得寫出 TI_AUTOPILOT_* 完整變數名 ----
def test_checklist_no_autopilot_varnames():
    block = _checklist_block()
    # 剝除 HTML 維護註解（其內刻意提及 TI_AUTOPILOT_* 作為紅線提醒，非實際變數使用）
    block = re.sub(r"<!--.*?-->", "", block, flags=re.DOTALL)
    assert (
        "TI_AUTOPILOT_" not in block
    ), "checklist 不得出現 TI_AUTOPILOT_* 完整變數名（首現須留在『設定』表）"
