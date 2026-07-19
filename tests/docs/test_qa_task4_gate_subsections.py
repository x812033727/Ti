"""任務 #4 驗收：「登入 / 門禁」段把門禁前置切成兩小節。

對應 PM 驗收標準 4（與 5、6 的相關紅線）：
- (A) 登入門禁＝設 TI_ACCESS_PASSWORD（最小啟用）。
- (B) Autopilot 門禁前置＝先設分支保護/ruleset 並把 CI 的 lint/test/sandbox-test 設為 required checks。
- (B) 小節只用簡稱、不寫 TI_AUTOPILOT_* 完整變數名（首現須留在「設定」表），且以連結指向設定表。
- 三個 required check 名稱與 .github/workflows/ci.yml 的實際 job 對齊。
"""

import re

from _repo import REPO_ROOT

ROOT = REPO_ROOT
README = (ROOT / "README.md").read_text(encoding="utf-8")
CI_YML = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")


def _gate_section() -> str:
    """擷取 ### 登入 / 門禁 段（到下一個同級 ### 之前）。"""
    m = re.search(r"^###\s+登入\s*/\s*門禁.*?$(.*?)(?=^###\s)", README, re.MULTILINE | re.DOTALL)
    assert m, "找不到『### 登入 / 門禁』段"
    return m.group(1)


GATE = _gate_section()


def _strip_comments(s: str) -> str:
    return re.sub(r"<!--.*?-->", "", s, flags=re.DOTALL)


def _subsection(label: str) -> str:
    """擷取 #### (A)/(B) 小節到下一個 #### 之前。"""
    m = re.search(rf"^####\s+\({label}\).*?$(.*?)(?=^####\s|\Z)", GATE, re.MULTILINE | re.DOTALL)
    assert m, f"找不到 #### ({label}) 小節"
    return m.group(1)


# ---- 兩小節 (A)/(B) 皆存在且 A 在 B 之前 ----
def test_two_subsections_exist():
    a = re.search(r"^####\s+\(A\)", GATE, re.MULTILINE)
    b = re.search(r"^####\s+\(B\)", GATE, re.MULTILINE)
    assert a, "缺 #### (A) 小節"
    assert b, "缺 #### (B) 小節"
    assert a.start() < b.start(), "(A) 應排在 (B) 之前"


# ---- (A) 登入門禁：最小啟用＝設 TI_ACCESS_PASSWORD ----
def test_subsection_a_login_gate():
    a = _subsection("A")
    assert "TI_ACCESS_PASSWORD" in a, "(A) 小節未提及 TI_ACCESS_PASSWORD"
    assert re.search(r"TI_ACCESS_PASSWORD=.*-m studio\.server", a), (
        "(A) 小節缺最小啟用範例（TI_ACCESS_PASSWORD=... -m studio.server）"
    )


# ---- (B) Autopilot 門禁前置：分支保護/ruleset ----
def test_subsection_b_branch_protection():
    b = _subsection("B")
    assert ("分支保護" in b) or ("branch protection" in b), "(B) 缺『分支保護/branch protection』"
    assert "ruleset" in b, "(B) 缺『ruleset』"


# ---- (B) required checks 含 CI 三個 job 名稱 ----
def test_subsection_b_required_checks():
    b = _subsection("B")
    assert "required" in b.lower() and "check" in b.lower(), "(B) 缺『required checks』概念"
    for job in ("lint", "test", "sandbox-test"):
        assert job in b, f"(B) required checks 缺 CI job：{job}"


# ---- required check 名稱與實際 CI job 對齊（防文件腐化） ----
def test_required_checks_match_ci_jobs():
    for job in ("lint:", "test:", "sandbox-test:"):
        assert re.search(rf"^  {re.escape(job)}", CI_YML, re.MULTILINE), (
            f"ci.yml 找不到 job 定義：{job}"
        )


# ---- (B) 不寫 TI_AUTOPILOT_* 完整變數名（紅線：首現須留在設定表） ----
def test_subsection_b_no_autopilot_varnames():
    b = _strip_comments(_subsection("B"))
    assert "TI_AUTOPILOT_" not in b, (
        "(B) 小節不應出現 TI_AUTOPILOT_* 完整變數名（首現須留在設定表）"
    )


# ---- (B) 以連結指向「設定」表，只連結不展開 ----
def test_subsection_b_links_to_settings():
    b = _subsection("B")
    assert "(#設定)" in b, "(B) 小節未以連結指向『[設定](#設定)』表"


# ---- 紅線守護：TI_AUTOPILOT_* 完整變數名首現仍在「設定」表（不早於門禁段） ----
def test_autopilot_varname_first_appearance_in_settings_table():
    for var in ("TI_AUTOPILOT_FORCE_PUSH",):
        first = README.find(var)
        assert first != -1, f"README 不再含 {var}"
        # 首現所在行應為設定表的表格行（以 | 起始）
        line = next(ln for ln in README.splitlines() if var in ln)
        assert line.lstrip().startswith("|"), f"{var} 首現不在設定表表格行：{line}"
