"""任務 #1 驗收測試：規則命中。

驗證 scripts/scan_shell_usage.sh 對兩類樣本各自產生警告：
  - `subprocess.run(..., shell=True)` → 由 Ruff S602/S604 命中
  - `asyncio.create_subprocess_shell(...)` → 由 grep/rg step 命中

設計：在 tmp 目錄放置可控樣本，並用「位置參數」把掃描目標指向 tmp，
與專案既有程式碼解耦，確保命中來自我們的樣本而非其他檔案。
"""

import subprocess
from pathlib import Path

import pytest
from _repo import REPO_ROOT

REPO = REPO_ROOT
SCRIPT = REPO / "scripts" / "scan_shell_usage.sh"

SHELL_TRUE_SAMPLE = "import subprocess\ndef run(cmd):\n    return subprocess.run(cmd, shell=True)\n"
CREATE_SHELL_SAMPLE = (
    "import asyncio\nasync def run(cmd):\n    return await asyncio.create_subprocess_shell(cmd)\n"
)
CLEAN_SAMPLE = "import subprocess\ndef run(cmd):\n    return subprocess.run(['echo', cmd])\n"


def run_scan(target: Path, mode: str = "warn"):
    """以指定目標跑掃描腳本，回傳 CompletedProcess。"""
    env = {"SCAN_MODE": mode, "PATH": __import__("os").environ["PATH"]}
    return subprocess.run(
        ["bash", str(SCRIPT), str(target)],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
    )


def test_script_exists_and_executable():
    assert SCRIPT.is_file(), f"找不到掃描腳本: {SCRIPT}"


def test_shell_true_hits_ruff(tmp_path):
    """shell=True 樣本應由 Ruff S602/S604 命中。"""
    (tmp_path / "sample.py").write_text(SHELL_TRUE_SAMPLE)
    cp = run_scan(tmp_path)
    out = cp.stdout + cp.stderr
    assert "S602" in out or "S604" in out, f"未命中 Ruff S602/S604:\n{out}"


def test_create_subprocess_shell_hits_grep(tmp_path):
    """create_subprocess_shell 樣本應由 grep/rg step 命中。"""
    (tmp_path / "sample.py").write_text(CREATE_SHELL_SAMPLE)
    cp = run_scan(tmp_path)
    out = cp.stdout + cp.stderr
    assert "create_subprocess_shell" in out, f"未命中 create_subprocess_shell:\n{out}"
    # 確認是 grep step 的命中（含檔名與行號）
    assert "sample.py" in out, f"grep 命中未含檔名/行號:\n{out}"


def test_both_in_one_target(tmp_path):
    """同一目標含兩類樣本時，兩種警告應同時出現。"""
    (tmp_path / "a.py").write_text(SHELL_TRUE_SAMPLE)
    (tmp_path / "b.py").write_text(CREATE_SHELL_SAMPLE)
    cp = run_scan(tmp_path)
    out = cp.stdout + cp.stderr
    assert "S602" in out or "S604" in out, f"缺 Ruff 命中:\n{out}"
    assert "create_subprocess_shell" in out, f"缺 grep 命中:\n{out}"


def test_clean_sample_no_false_positive(tmp_path):
    """安全寫法（list 形式、無 shell）不應誤報。"""
    (tmp_path / "clean.py").write_text(CLEAN_SAMPLE)
    cp = run_scan(tmp_path)
    out = cp.stdout + cp.stderr
    # 腳本在無命中時會印固定提示句；以提示句判定，避免 header 文字干擾。
    assert "（無 S602/S604/S605 命中）" in out, f"安全樣本誤報 Ruff:\n{out}"
    assert "（無 create_subprocess_shell 命中）" in out, f"安全樣本誤報 grep:\n{out}"


def test_warn_mode_returns_zero_even_on_hit(tmp_path):
    """warn 模式即使命中也回 0（與驗收 #2 相關，順帶確認不阻斷）。"""
    (tmp_path / "a.py").write_text(SHELL_TRUE_SAMPLE)
    (tmp_path / "b.py").write_text(CREATE_SHELL_SAMPLE)
    cp = run_scan(tmp_path, mode="warn")
    assert (
        cp.returncode == 0
    ), f"warn 模式命中後仍回非零: rc={cp.returncode}\n{cp.stdout}{cp.stderr}"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
