"""任務 #5 驗收測試：可重現 demo。

驗證能用單一指令 `bash scripts/scan_shell_usage.sh` 在本機跑出與 CI 相同的
掃描結果，且含至少一筆命中範例。

驗證面向：
  - 單一指令：無需額外參數即可跑（cwd=repo 根），exit 0（warn）
  - 至少一筆命中：輸出含既有 create_subprocess_shell 命中
  - 與 CI 相同：CI step 的 run 字串即此單一指令、SCAN_MODE 即此 demo 預設
  - 可重現：重複執行輸出穩定一致（去除無關雜訊後）
"""

import os
import re
import subprocess
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parents[1]
SCRIPT_REL = "scripts/scan_shell_usage.sh"
SCRIPT = REPO / SCRIPT_REL
CI_YML = REPO / ".github" / "workflows" / "ci.yml"

DEMO_CMD = ["bash", SCRIPT_REL]


def run_demo(env=None):
    return subprocess.run(
        DEMO_CMD,
        cwd=REPO,
        env=env or dict(os.environ),
        capture_output=True,
        text=True,
    )


def _ci_scan_step():
    data = yaml.safe_load(CI_YML.read_text())
    for job in data["jobs"].values():
        for step in job.get("steps", []):
            if SCRIPT_REL in step.get("run", ""):
                return step
    return None


# --- 單一指令可跑且不擋 ---------------------------------------------------


def test_single_command_runs_and_returns_zero():
    cp = run_demo()
    out = cp.stdout + cp.stderr
    assert cp.returncode == 0, f"demo 指令未回 0（warn 模式應恆 0）：{cp.returncode}\n{out}"
    assert "掃描完成" in out, f"demo 輸出缺完成標記，疑似未跑完：\n{out}"


# --- 至少一筆命中範例 ------------------------------------------------------


def test_demo_contains_at_least_one_hit():
    cp = run_demo()
    out = cp.stdout + cp.stderr
    # 既有 studio/runner.py 有 create_subprocess_shell，demo 必命中至少這一筆
    assert "create_subprocess_shell" in out, f"demo 未含任何命中範例：\n{out}"
    assert "runner.py" in out, f"demo 命中未指向具體檔案（缺檔名）：\n{out}"


# --- 與 CI 相同：指令與模式一致 -------------------------------------------


def test_ci_uses_the_same_single_command():
    step = _ci_scan_step()
    assert step is not None, "ci.yml 找不到掃描 step"
    run_str = step["run"].strip()
    # CI 跑的就是這支單一指令（允許前後空白），無額外參數差異
    assert run_str == f"bash {SCRIPT_REL}", f"CI run 與 demo 單一指令不符：{run_str!r}"
    # CI 的 SCAN_MODE 與 demo 預設（warn）一致
    ci_mode = step.get("env", {}).get("SCAN_MODE", "warn")
    assert ci_mode == "warn", f"CI SCAN_MODE 非 warn，與 demo 預設不一致：{ci_mode}"


def test_demo_output_matches_ci_invocation():
    """以 CI 完全相同的 env 跑 demo，與本機裸跑結果一致（命中特徵 + exit code）。"""
    ci_env = dict(os.environ)
    ci_env["SCAN_MODE"] = "warn"  # CI step env
    ci_like = run_demo(ci_env)
    local = run_demo()  # 本機預設（腳本內預設亦 warn）

    def feat(cp):
        text = cp.stdout + cp.stderr
        return (
            cp.returncode,
            "create_subprocess_shell" in text,
            bool(re.search(r"runner\.py:\d+", text)),
        )

    assert feat(ci_like) == feat(local), (
        f"CI 跑法與本機 demo 結果不一致：CI={feat(ci_like)} local={feat(local)}\n"
        f"--- CI-like ---\n{ci_like.stdout}{ci_like.stderr}\n"
        f"--- local ---\n{local.stdout}{local.stderr}"
    )


# --- 可重現：多次執行穩定一致 ---------------------------------------------


def _normalize(text):
    """去除無關雜訊（本例輸出本就無時間戳，主要為防護未來變動）。"""
    return "\n".join(line.rstrip() for line in text.splitlines())


def test_demo_is_reproducible_across_runs():
    out1 = _normalize(run_demo().stdout)
    out2 = _normalize(run_demo().stdout)
    assert out1 == out2, (
        f"demo 兩次執行輸出不一致（不可重現）：\n--- 第一次 ---\n{out1}\n--- 第二次 ---\n{out2}"
    )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
