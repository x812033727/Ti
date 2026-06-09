"""任務 #1 驗收：README「執行環境前置」段落三步驟 + 整體驗收標準核對。

純文字結構檢查（不需網路），對應 PM 驗收標準 1~5。
"""

import re

from _repo import REPO_ROOT

ROOT = REPO_ROOT
README = (ROOT / "README.md").read_text(encoding="utf-8")
GITIGNORE = (ROOT / ".gitignore").read_text(encoding="utf-8")
PYPROJECT = (ROOT / "pyproject.toml").read_text(encoding="utf-8")


def _section(title: str) -> str:
    """擷取某個 ## 標題到下一個 ## 之間的內容。"""
    m = re.search(
        rf"^##\s+{re.escape(title)}\s*$(.*?)(?=^##\s|\Z)", README, re.MULTILINE | re.DOTALL
    )
    assert m, f"找不到段落: ## {title}"
    return m.group(1)


# ---- 驗收標準 1：獨立「執行環境前置」段落，含三步驟 ----
def test_section_exists():
    assert re.search(r"^##\s+執行環境前置\s*$", README, re.MULTILINE)


def test_step_create_venv():
    sec = _section("執行環境前置")
    assert "python3 -m venv .venv" in sec


def test_step_activate_cross_platform():
    sec = _section("執行環境前置")
    assert "source .venv/bin/activate" in sec, "缺 mac/Linux 啟動"
    assert r".venv\Scripts\activate" in sec, "缺 Windows 啟動"


def test_step_install_extras():
    sec = _section("執行環境前置")
    assert re.search(r'install\s+-e\s+"\.\[dev\]"', sec), "缺 install -e .[dev]"
    assert ".[openai]" in sec, "缺 .[openai] 選用標注"
    # openai 必須被標為選用
    assert "選用" in sec


# ---- 驗收標準 2：至少一處 .venv/bin/python3 完整路徑（免 activate），與 studio.server 一致 ----
def test_full_path_python_usage():
    assert ".venv/bin/python3" in README


def test_studio_server_entry_consistent():
    # pyproject 入口為 studio.server，README 須有對應的 -m studio.server 範例
    assert "studio.server" in README


# ---- 驗收標準 3：一行可複製驗證指令 + 預期輸出 ----
def test_verify_command_present():
    sec = _section("執行環境前置")
    assert ".venv/bin/python3 -c \"import studio; print('ok')\"" in sec
    # 預期輸出 ok 要寫明
    assert "ok" in sec


# ---- 驗收標準 4：Python ≥3.10 + .venv 不進版控 ----
def test_python_version_noted():
    sec = _section("執行環境前置")
    assert re.search(r"3\.10", sec), "前置段未標明 Python 3.10"
    # 與 pyproject 對齊
    assert 'requires-python = ">=3.10"' in PYPROJECT


def test_venv_gitignored():
    sec = _section("執行環境前置")
    assert ".gitignore" in sec, "前置段未提及 .gitignore"
    assert ".venv/" in GITIGNORE, ".venv 未實際列入 .gitignore"


# ---- 驗收標準 5：每步附「預期結果」 ----
def test_expected_results_for_each_step():
    sec = _section("執行環境前置")
    # 至少 4 個「預期結果」（建立 / 啟動 / 安裝 / 驗證）
    assert sec.count("預期結果") >= 4, f"預期結果數量不足: {sec.count('預期結果')}"


# ---- 驗收標準 5：既有段落不得殘留與新段落矛盾的裸 pip fallback ----
def test_no_bare_pip_fallback_conflict():
    # 設計決策明確要求刪除 line47 的裸 pip + 繞過 extras 的 fallback
    bad = re.findall(r"pip install -e \.\s+#\s*或：pip install claude-agent-sdk", README)
    assert not bad, f"殘留與新段落矛盾的裸 pip fallback: {bad}"
