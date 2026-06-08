"""QA 驗收測試：文件執行指令統一為 .venv/bin/python -m pytest。

逐條對照任務驗收標準 1~7。敘述性提及（非可複製執行指令）保留。
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
CONTRIB = ROOT / "CONTRIBUTING.md"
INVENTORY = ROOT / "studio" / "docs" / "subprocess_migration_inventory.md"


def lines(p: Path) -> list[str]:
    return p.read_text(encoding="utf-8").splitlines()


# 標準 1：README 不再有單獨成行的執行指令 `pytest`
def test_readme_no_bare_pytest_command():
    bad = [(i + 1, ln) for i, ln in enumerate(lines(README)) if re.match(r"^\s*pytest(\s|$)", ln)]
    assert not bad, f"README 仍有裸 pytest 執行指令: {bad}"


# 標準 2：CONTRIBUTING:26、:60 均為 .venv/bin/python -m pytest -q
def test_contributing_pytest_prefix():
    text = CONTRIB.read_text(encoding="utf-8")
    occ = re.findall(r"`?\.venv/bin/python -m pytest -q`?", text)
    assert len(occ) >= 2, f"CONTRIBUTING 應有 >=2 處 .venv/bin/python -m pytest -q，實得 {len(occ)}"
    # 不應再有以 python 開頭的 pytest 執行指令
    bad = [
        (i + 1, ln)
        for i, ln in enumerate(lines(CONTRIB))
        if re.search(r"(?<![\w./-])python -m pytest", ln)
    ]
    assert not bad, f"CONTRIBUTING 仍有 'python -m pytest'（未補 .venv 前綴）: {bad}"


# 標準 3：CONTRIBUTING 建 venv 指令為 python3 -m venv .venv
def test_contributing_venv_python3():
    text = CONTRIB.read_text(encoding="utf-8")
    assert "python3 -m venv .venv" in text, "建 venv 指令應為 python3 -m venv .venv"
    bad = re.search(r"(?<![\w3])python -m venv", text)
    assert not bad, "CONTRIBUTING 仍有 'python -m venv'（應改 python3）"


# 標準 4：文件中所有 pytest「執行指令」前綴統一 .venv/bin/python -m
def test_all_pytest_run_commands_prefixed():
    bad = []
    for p in (README, CONTRIB):
        for i, ln in enumerate(lines(p)):
            # 抓「執行指令」: 行內含 -m pytest 或行首 pytest，但前綴不是 .venv/bin/python
            if re.search(r"(?<![\w./-])python -m pytest", ln):
                bad.append((p.name, i + 1, ln))
            if re.match(r"^\s*pytest(\s|$)", ln):
                bad.append((p.name, i + 1, ln))
    assert not bad, f"仍有未統一前綴的 pytest 執行指令: {bad}"


# 標準 5：已處理 Windows 跨平台（加註路徑 或 聲明適用平台）
def test_windows_cross_platform_noted():
    win_tokens = (r".venv\Scripts", "Linux", "macOS", "Windows")
    hit = False
    for p in (README, CONTRIB):
        t = p.read_text(encoding="utf-8")
        if any(tok in t for tok in win_tokens):
            hit = True
    assert hit, "文件未處理 Windows 跨平台（無路徑加註，也無適用平台聲明）"


# 標準 6：inventory 未被更動
def test_inventory_untouched():
    assert INVENTORY.exists(), "inventory 檔不應消失"
    r = subprocess.run(
        ["git", "status", "--short", str(INVENTORY)],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert r.stdout.strip() == "", f"inventory 不應被更動: {r.stdout!r}"


# 標準 7：驗收指令路徑有效（.venv/bin/python 存在且可呼叫）
def test_venv_python_exists_and_runs():
    py = ROOT / ".venv" / "bin" / "python"
    if not py.exists():
        # CI（actions/setup-python 直跑）等環境不依文件建立 .venv；此檢查只在
        # 依 CONTRIBUTING 建好 .venv 的環境（本地 / autopilot gate）才有意義。
        pytest.skip(".venv 未建立（如 CI 用 setup-python 直跑），略過驗收指令路徑檢查")
    r = subprocess.run([str(py), "--version"], capture_output=True, text=True)
    assert r.returncode == 0, f".venv/bin/python 無法執行: {r.stderr}"
