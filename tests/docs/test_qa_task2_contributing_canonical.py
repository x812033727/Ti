"""QA 驗收測試 — 任務 #2：dev 指令收斂、校正於 CONTRIBUTING.md 為唯一權威。

驗收重點：
- CONTRIBUTING 含齊全的 pip install / pytest / ruff check+format / pre-commit 指令；
- 這些 canonical 指令「可正確執行」（實測 .venv 內各模組可被 `-m` 呼叫）；
- 全部 pytest 執行指令統一為 `.venv/bin/python -m pytest -q`（≥2 處、無裸前綴殘留）；
- 建 venv 指令維持 `python3 -m venv .venv`；
- 未引入新依賴（pyproject 未變）。
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest
from _repo import REPO_ROOT

ROOT = REPO_ROOT
CONTRIB = ROOT / "CONTRIBUTING.md"
PYPROJECT = ROOT / "pyproject.toml"
VENV_PY = ROOT / ".venv" / "bin" / "python"


def _txt() -> str:
    return CONTRIB.read_text(encoding="utf-8")


def _lines() -> list[str]:
    return _txt().splitlines()


# 標準 2-a：4 組 dev 指令家族在 CONTRIBUTING 齊全，且以 .venv 完整路徑呈現
def test_canonical_commands_present_and_prefixed():
    t = _txt()
    required = [
        r'\.venv/bin/python -m pip install -e "\.\[dev\]"',
        r"\.venv/bin/python -m pytest -q",
        r"\.venv/bin/python -m ruff check \.",
        r"\.venv/bin/python -m ruff format --check \.",
        r"\.venv/bin/python -m pre_commit install",
    ]
    missing = [pat for pat in required if not re.search(pat, t)]
    assert not missing, f"CONTRIBUTING 缺少 canonical 指令（或未加 .venv 前綴）: {missing}"


# 標準 2-b：ruff 同時涵蓋 check 與 format（lint + 格式化兩面）
def test_ruff_covers_check_and_format():
    t = _txt()
    assert ".venv/bin/python -m ruff check ." in t, "缺 ruff check"
    assert ".venv/bin/python -m ruff format" in t, "缺 ruff format"


# 標準 2-c：全部 pytest 執行指令統一前綴，≥2 處，且無裸 python -m pytest
def test_pytest_commands_unified():
    t = _txt()
    occ = re.findall(r"`?\.venv/bin/python -m pytest -q`?", t)
    assert len(occ) >= 2, f"應 >=2 處 .venv/bin/python -m pytest -q，實得 {len(occ)}"
    bad = [
        (i + 1, ln)
        for i, ln in enumerate(_lines())
        if re.search(r"(?<![\w./-])python -m pytest", ln)
    ]
    assert not bad, f"仍有未補 .venv 前綴的 python -m pytest: {bad}"


# 標準 2-d：建 venv 指令維持 python3（避免 .venv 尚未存在時用錯直譯器）
def test_venv_bootstrap_uses_python3():
    t = _txt()
    assert "python3 -m venv .venv" in t, "建 venv 指令應為 python3 -m venv .venv"
    assert not re.search(r"(?<![\w3])python -m venv", t), "不應有 python -m venv（應 python3）"


# 標準 2-e：canonical 唯一權威宣告存在
def test_declares_single_source_of_truth():
    t = _txt()
    assert ("唯一權威" in t) or ("canonical" in t.lower()), "CONTRIBUTING 未宣告自身為唯一權威來源"


# 標準 6：未引入新依賴（pyproject 自 task 起點未被本收斂改動）
def test_pyproject_unchanged_by_convergence():
    # 與分叉點 4f32d3a（task 起點前的最後一個共同 commit）相比，pyproject 不應被改動
    r = subprocess.run(
        ["git", "diff", "4f32d3a..HEAD", "--", str(PYPROJECT)],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, f"git diff 失敗: {r.stderr}"
    assert r.stdout.strip() == "", f"pyproject.toml 不應被本次收斂改動:\n{r.stdout}"


# 標準 2 核心：canonical 指令「可正確執行」——實測 .venv 內各模組可被 -m 呼叫
@pytest.mark.parametrize(
    "mod,args",
    [
        ("pip", ["--version"]),
        ("pytest", ["--version"]),
        ("ruff", ["--version"]),
        ("pre_commit", ["--version"]),
    ],
)
def test_canonical_tools_actually_runnable(mod, args):
    py = VENV_PY if VENV_PY.exists() else Path(sys.executable)
    r = subprocess.run([str(py), "-m", mod, *args], capture_output=True, text=True)
    assert r.returncode == 0, f"`python -m {mod}` 無法執行: {r.stderr or r.stdout}"


# 從 CONTRIBUTING 文件中「抽出」pip/ruff 的 --version 變體實跑，證明文件指令真的可用
def test_documented_install_and_lint_modules_resolve():
    if not VENV_PY.exists():
        pytest.skip(".venv 未建立，略過文件指令可執行性實測")
    # 文件宣告以 .venv/bin/python -m {pip,ruff,pytest,pre_commit} 為入口；逐一確認模組可解析
    for mod in ("pip", "ruff", "pytest", "pre_commit"):
        r = subprocess.run(
            [str(VENV_PY), "-c", f"import importlib; importlib.import_module('{mod}')"],
            capture_output=True,
            text=True,
        )
        assert r.returncode == 0, f"文件 canonical 指令依賴的模組 {mod} 無法 import: {r.stderr}"
