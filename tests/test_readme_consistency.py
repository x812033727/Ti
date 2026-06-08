"""任務 #4 驗收（驗收標準 5 後半 + 設計決策「同步改寫各段」）：
校對 README 既有「安裝/啟動/測試/切換 OpenAI/離線示範」各段與新「執行環境前置」
段落一致、無矛盾——統一為 .venv 完整路徑寫法，無殘留裸 python/pip 指令。
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
README = (ROOT / "README.md").read_text(encoding="utf-8")


def _code_lines() -> list[str]:
    """擷取所有 ``` code block 內的行（指令才是校對對象，避免誤判內文敘述）。"""
    lines, in_block = [], False
    for ln in README.splitlines():
        if ln.lstrip().startswith("```"):
            in_block = not in_block
            continue
        if in_block:
            lines.append(ln)
    return lines


CODE = _code_lines()
CODE_TEXT = "\n".join(CODE)


def test_no_bare_python_studio_server():
    """啟動指令一律 .venv 完整路徑，不得有裸 `python -m studio.server`。
    （允許 Windows 的 .venv\\Scripts\\python 與 mac/Linux 的 .venv/bin/python3）"""
    bad = [ln for ln in CODE if re.search(r"(?<![/\\])\bpython3? -m studio", ln)]
    assert not bad, f"殘留裸 python 啟動指令: {bad}"


def test_no_bare_pip_install():
    """安裝指令一律 .venv 完整路徑，不得有裸 `pip install`。"""
    # 裸 = pip 前非「-m 」（排除合法的 python3 -m pip）且非路徑字元
    bad = [ln for ln in CODE if re.search(r"(?<!-m )(?<![/\\.])\bpip install", ln)]
    assert not bad, f"殘留裸 pip install: {bad}"


def test_no_bare_uvicorn_command():
    """uvicorn 啟動也須走 .venv（不得裸 `uvicorn studio.server:app`）。"""
    # 裸 = uvicorn 前非「-m 」（排除合法的 python3 -m uvicorn）
    bad = [ln for ln in CODE if re.search(r"(?<!-m )(?<![./\\])\buvicorn studio", ln)]
    assert not bad, f"殘留裸 uvicorn 指令: {bad}"


def test_all_server_launches_use_venv_path():
    """所有 studio.server 啟動皆採 .venv 路徑（mac/Linux 或 Windows 皆可）。"""
    launches = [
        ln for ln in CODE if "studio.server" in ln and "-m" in ln and not ln.strip().startswith("#")
    ]
    assert launches, "找不到任何 studio.server 啟動範例"
    for ln in launches:
        assert re.search(r"\.venv[/\\](bin/python3|Scripts\\python)", ln), (
            f"啟動指令未用 .venv 完整路徑: {ln}"
        )


def test_install_section_points_to_env_setup():
    """既有『安裝』段不得再給出與前置段矛盾的獨立安裝指令，
    而應導向/沿用前置段第 3 步的 .venv 寫法。"""
    m = re.search(r"^##\s+安裝\s*$(.*?)(?=^##\s|\Z)", README, re.MULTILINE | re.DOTALL)
    assert m, "找不到『安裝』段"
    sec = m.group(1)
    # 安裝段不得殘留裸 `pip install -e .` fallback
    assert not re.search(r"(?<![/\\.])\bpip install -e \.", sec), "安裝段殘留裸 pip install -e ."
    # 應引用前置段（連結或 .venv 寫法）
    assert "執行環境前置" in sec or ".venv/bin/python3" in sec, "安裝段未對齊/引用前置段"


def test_openai_section_consistent():
    """切換 OpenAI 段安裝與啟動皆 .venv 寫法。"""
    m = re.search(r"切換到 OpenAI.*?(?=^##\s|\Z)", README, re.MULTILINE | re.DOTALL)
    assert m, "找不到『切換到 OpenAI』段"
    sec = m.group(0)
    assert '.venv/bin/python3 -m pip install -e ".[openai]"' in sec
    assert ".venv/bin/python3 -m studio.server" in sec


def test_windows_path_parallel_for_launch():
    """啟動段比照設計決策並列 Windows 路徑對應。"""
    assert r".venv\Scripts\python -m studio.server" in README
