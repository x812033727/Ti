"""任務 #5 驗收（驗收標準 1）：README 有獨立「執行環境前置」段落，
完整包含四要素——建立 venv、跨平台啟動（mac/Linux + Windows）、
安裝 .[dev]、並標注 .[openai] 為選用。
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
README = (ROOT / "README.md").read_text(encoding="utf-8")


def _env_section() -> str:
    m = re.search(r"^(##\s+執行環境前置\s*$.*?)(?=^##\s|\Z)",
                  README, re.MULTILINE | re.DOTALL)
    assert m, "找不到獨立『## 執行環境前置』段落"
    return m.group(1)


SEC = _env_section()


def test_is_independent_h2_section():
    """必須是獨立的 ## 二級標題段落（非塞在其他段內）。"""
    assert re.search(r"^##\s+執行環境前置\s*$", README, re.MULTILINE)


def test_contains_create_venv():
    assert "python3 -m venv .venv" in SEC


def test_contains_activate_mac_linux():
    assert "source .venv/bin/activate" in SEC


def test_contains_activate_windows():
    assert r".venv\Scripts\activate" in SEC


def test_contains_install_dev_extra():
    assert re.search(r'pip install -e\s+"\.\[dev\]"', SEC), "缺 install -e .[dev]"


def test_openai_marked_optional():
    """.[openai] 必須出現且明確標為『選用』。"""
    assert ".[openai]" in SEC, "缺 .[openai]"
    # 同一行或鄰近須有『選用』字樣
    for line in SEC.splitlines():
        if ".[openai]" in line and "選用" in line:
            return
    # 退而求其次：openai 行的後續 80 字內含『選用』
    idx = SEC.find(".[openai]")
    assert "選用" in SEC[idx - 40: idx + 80], ".[openai] 未標注為選用"


def test_section_order_create_then_activate_then_install():
    """段內順序：建立 → 啟動 → 安裝（符合設計決策的步驟序）。"""
    i_create = SEC.find("python3 -m venv .venv")
    i_activate = SEC.find("source .venv/bin/activate")
    i_install = SEC.find('pip install -e ".[dev]"')
    assert -1 < i_create < i_activate < i_install, \
        f"步驟順序錯誤: create={i_create}, activate={i_activate}, install={i_install}"


def test_section_located_between_roles_and_install():
    """設計決策：段落位於『## 角色』與『## 安裝』之間。"""
    i_roles = README.find("## 角色")
    i_env = README.find("## 執行環境前置")
    i_install = README.find("## 安裝")
    assert -1 < i_roles < i_env < i_install, \
        f"段落位置不符: roles={i_roles}, env={i_env}, install={i_install}"
