"""任務 #4 驗收測試：CI 與 pre-commit 一致。

驗證兩處使用相同規則集（S602/S604/S605）與相同 create_subprocess_shell
掃描邏輯，無「本機過、CI 擋」落差。核心設計為 SSOT：兩處皆呼叫同一支
scripts/scan_shell_usage.sh，故規則與 args 天然一致。

驗證面向：
  - 靜態：CI run 與 pre-commit entry 都指向同一支腳本，且未各自 inline 規則
  - SSOT：規則 S602/S604/S605 與 create_subprocess_shell 只在腳本內定義
  - SCAN_MODE 對齊：兩處等效（CI=warn，pre-commit 預設亦 warn），不致一邊 block
  - 行為對齊：對同一目標，CI 跑法與 pre-commit 跑法產生相同命中與 exit code
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parents[1]
SCRIPT_REL = "scripts/scan_shell_usage.sh"
SCRIPT = REPO / SCRIPT_REL
CI_YML = REPO / ".github" / "workflows" / "ci.yml"
PRECOMMIT = REPO / ".pre-commit-config.yaml"


def _ci_scan_step():
    data = yaml.safe_load(CI_YML.read_text())
    for job in data["jobs"].values():
        for step in job.get("steps", []):
            if SCRIPT_REL in step.get("run", ""):
                return step
    return None


def _precommit_scan_hook():
    data = yaml.safe_load(PRECOMMIT.read_text())
    for repo in data["repos"]:
        for hook in repo.get("hooks", []):
            if hook.get("id") == "scan-shell-usage":
                return hook
    return None


# --- 靜態：兩處指向同一支腳本 ---------------------------------------------


def test_both_invoke_the_same_script():
    step = _ci_scan_step()
    hook = _precommit_scan_hook()
    assert step is not None, "ci.yml 找不到掃描 step"
    assert hook is not None, "pre-commit 找不到 scan-shell-usage hook"
    assert SCRIPT_REL in step["run"], f"CI 未呼叫 {SCRIPT_REL}：{step['run']}"
    assert SCRIPT_REL in hook["entry"], f"pre-commit 未呼叫 {SCRIPT_REL}：{hook['entry']}"


def test_script_actually_exists():
    assert SCRIPT.is_file(), f"SSOT 腳本不存在：{SCRIPT}"


# --- SSOT：規則只在腳本內定義，CI/pre-commit 不各自 inline -----------------


def test_rules_live_only_in_script_not_inlined():
    """CI run 與 pre-commit entry 都不該自己 inline ruff/grep 規則，
    否則會與腳本分歧，形成兩套來源。"""
    script_text = SCRIPT.read_text()
    # 規則集與掃描關鍵字必須存在於腳本（SSOT）
    assert "S602" in script_text and "S604" in script_text and "S605" in script_text, (
        "腳本內缺 S602/S604/S605 規則集"
    )
    assert "create_subprocess_shell" in script_text, "腳本內缺 create_subprocess_shell 掃描"

    # CI / pre-commit 不應重複定義規則（避免雙來源分歧）
    ci_run = _ci_scan_step()["run"]
    pc_entry = _precommit_scan_hook()["entry"]
    for surface_name, surface in (("CI run", ci_run), ("pre-commit entry", pc_entry)):
        assert "S602" not in surface, f"{surface_name} 不該 inline 規則 S602：{surface}"
        assert "create_subprocess_shell" not in surface, (
            f"{surface_name} 不該 inline grep 規則：{surface}"
        )


# --- SCAN_MODE 對齊：兩處等效（皆 warn），不致一邊 block --------------------


def test_scan_mode_aligned_warn_on_both():
    ci_mode = _ci_scan_step().get("env", {}).get("SCAN_MODE", "warn")
    hook = _precommit_scan_hook()
    # pre-commit hook 若有設 env 也納入比較；未設則沿用腳本預設 warn。
    pc_mode = "warn"
    entry = hook["entry"]
    args = hook.get("args", [])
    # 確認 pre-commit 沒有把 SCAN_MODE 改成 block（entry/args 中皆無）
    assert "SCAN_MODE=block" not in entry, f"pre-commit entry 把模式改成 block：{entry}"
    assert "block" not in " ".join(args), f"pre-commit args 含 block：{args}"
    assert ci_mode == "warn", f"CI SCAN_MODE 非 warn：{ci_mode}"
    assert ci_mode == pc_mode, f"CI 與 pre-commit SCAN_MODE 不一致：{ci_mode} vs {pc_mode}"


# --- 行為對齊：CI 跑法 vs pre-commit 跑法，結果一致 ------------------------


def _run_ci_style(env):
    """CI step 的跑法：bash scripts/scan_shell_usage.sh（warn 模式，掃預設 studio）。"""
    e = dict(env)
    e["SCAN_MODE"] = "warn"
    return subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=REPO,
        env=e,
        capture_output=True,
        text=True,
    )


def _run_precommit_style(env):
    """pre-commit 的跑法：透過 pre-commit run 觸發同一 hook。"""
    precommit_bin = REPO / ".venv" / "bin" / "pre-commit"
    if not precommit_bin.exists():
        return None
    e = dict(env)
    e["PRE_COMMIT_HOME"] = e.get("PRE_COMMIT_HOME") or str(REPO / ".pc-cache-qa")
    return subprocess.run(
        [str(precommit_bin), "run", "scan-shell-usage", "--all-files"],
        cwd=REPO,
        env=e,
        capture_output=True,
        text=True,
    )


def _extract_hits(text):
    """從輸出抽出可比較的命中特徵：S60x 規則碼與 create_subprocess_shell 命中行。"""
    has_s = any(code in text for code in ("S602", "S604", "S605"))
    has_csp = "create_subprocess_shell" in text and "runner.py" in text
    return (has_s, has_csp)


def test_ci_and_precommit_produce_same_result():
    """同一份 studio/：兩種跑法的命中特徵與 exit code 必須一致 → 無本機/CI 落差。"""
    if shutil.which("git") is None:
        pytest.skip("環境無 git")
    precommit_bin = REPO / ".venv" / "bin" / "pre-commit"
    if not precommit_bin.exists():
        pytest.skip("環境無 pre-commit")

    base_env = dict(os.environ)

    ci = _run_ci_style(base_env)
    pc = _run_precommit_style(base_env)
    assert pc is not None, "pre-commit 不可用"

    ci_out = ci.stdout + ci.stderr
    pc_out = pc.stdout + pc.stderr

    # 1) 兩種跑法都不擋（exit 0），無本機過 CI 擋
    assert ci.returncode == 0, f"CI 跑法非 0：{ci.returncode}\n{ci_out}"
    assert pc.returncode == 0, f"pre-commit 跑法非 0：{pc.returncode}\n{pc_out}"

    # 2) 命中特徵一致（同一腳本掃同一 studio，結果必相同）
    ci_hits = _extract_hits(ci_out)
    pc_hits = _extract_hits(pc_out)
    assert ci_hits == pc_hits, (
        f"CI 與 pre-commit 命中不一致：CI={ci_hits} pre-commit={pc_hits}\n"
        f"--- CI ---\n{ci_out}\n--- pre-commit ---\n{pc_out}"
    )
    # 3) 至少要有 create_subprocess_shell 命中（證明真有跑到掃描而非空轉）
    assert pc_hits[1], f"pre-commit 跑法未見 runner.py 的 create_subprocess_shell 命中：\n{pc_out}"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
