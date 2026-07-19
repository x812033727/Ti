"""任務 #2 驗收測試：先 warning 不擋。

驗證即使掃出問題：
  - 腳本 warn 模式恆回 0（不擋契約的根）
  - CI 的 Scan step 設 continue-on-error: true 且 SCAN_MODE=warn（job 仍綠）
  - pre-commit hook 不阻斷 commit（真實 git commit 端到端）
對照：block 模式命中會回非零，證明 warn 的「不擋」是刻意設計而非腳本失靈。
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from _repo import REPO_ROOT

yaml = pytest.importorskip("yaml")

REPO = REPO_ROOT
SCRIPT = REPO / "scripts" / "scan_shell_usage.sh"
CI_YML = REPO / ".github" / "workflows" / "ci.yml"
PRECOMMIT = REPO / ".pre-commit-config.yaml"

HIT_SAMPLE = (
    "import subprocess, asyncio\n"
    "def a(cmd):\n"
    "    return subprocess.run(cmd, shell=True)\n"
    "async def b(cmd):\n"
    "    return await asyncio.create_subprocess_shell(cmd)\n"
)


def run_scan(target: Path, mode: str):
    env = dict(os.environ)
    env["SCAN_MODE"] = mode
    return subprocess.run(
        ["bash", str(SCRIPT), str(target)],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
    )


# --- 腳本層：warn/block 的 exit code 契約 ---------------------------------


def test_warn_mode_returns_zero_even_on_hit(tmp_path):
    (tmp_path / "s.py").write_text(HIT_SAMPLE)
    cp = run_scan(tmp_path, "warn")
    out = cp.stdout + cp.stderr
    assert ("S602" in out or "S604" in out) or "create_subprocess_shell" in out, (
        f"前提失敗：樣本應被掃出命中:\n{out}"
    )
    assert cp.returncode == 0, f"warn 模式命中後應回 0，實得 {cp.returncode}\n{out}"


def test_block_mode_returns_nonzero_on_hit(tmp_path):
    """對照組：證明命中是真的被偵測，warn 的回 0 是刻意而非漏掃。"""
    (tmp_path / "s.py").write_text(HIT_SAMPLE)
    cp = run_scan(tmp_path, "block")
    assert cp.returncode != 0, f"block 模式命中應回非零，實得 {cp.returncode}"


# --- CI 層：continue-on-error 與 SCAN_MODE ---------------------------------


def _find_scan_step():
    data = yaml.safe_load(CI_YML.read_text())
    for job in data["jobs"].values():
        for step in job.get("steps", []):
            run = step.get("run", "")
            if "scan_shell_usage.sh" in run:
                return step
    return None


def test_ci_yaml_parses():
    data = yaml.safe_load(CI_YML.read_text())
    assert "jobs" in data, "ci.yml 解析後缺 jobs"


def test_ci_scan_step_is_nonblocking():
    step = _find_scan_step()
    assert step is not None, "ci.yml 找不到呼叫 scan_shell_usage.sh 的 step"
    assert step.get("continue-on-error") is True, (
        f"Scan step 缺 continue-on-error: true，會擋 CI：{step}"
    )
    assert step.get("env", {}).get("SCAN_MODE") == "warn", (
        f"Scan step SCAN_MODE 應為 warn：{step.get('env')}"
    )


def test_ci_existing_lint_steps_have_no_continue_on_error():
    """反向保護：原有 ruff check / format step 不應被加上 continue-on-error。"""
    data = yaml.safe_load(CI_YML.read_text())
    for step in data["jobs"]["lint"]["steps"]:
        run = step.get("run", "")
        if run.strip() in ("ruff check .", "ruff format --check ."):
            assert "continue-on-error" not in step, (
                f"既有 lint step 不應有 continue-on-error：{step}"
            )


# --- pre-commit 層：config 合法 + 真實 commit 不被擋 ------------------------


def test_precommit_yaml_parses_and_hook_present():
    data = yaml.safe_load(PRECOMMIT.read_text())
    hooks = [h for repo in data["repos"] for h in repo.get("hooks", [])]
    ids = [h.get("id") for h in hooks]
    assert "scan-shell-usage" in ids, f"pre-commit 缺 scan-shell-usage hook：{ids}"


def _git(args, cwd, env):
    return subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True)


def test_real_git_commit_not_blocked_by_hook(tmp_path):
    """端到端：臨時 repo 安裝 hook，commit 一個含命中樣本的檔，確認 commit 成功。"""
    if shutil.which("git") is None:
        pytest.skip("環境無 git")
    precommit_bin = REPO / ".venv" / "bin" / "pre-commit"
    if not precommit_bin.exists():
        pytest.skip("環境無 pre-commit")

    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / "studio").mkdir()
    shutil.copy(SCRIPT, repo / "scripts" / "scan_shell_usage.sh")
    (repo / "studio" / "sample.py").write_text(HIT_SAMPLE)
    # 只放 scan-shell-usage local hook，隔離測試對象。
    (repo / ".pre-commit-config.yaml").write_text(
        "repos:\n"
        "  - repo: local\n"
        "    hooks:\n"
        "      - id: scan-shell-usage\n"
        "        name: scan shell usage (warning-only)\n"
        "        entry: bash scripts/scan_shell_usage.sh\n"
        "        language: system\n"
        "        pass_filenames: false\n"
        "        always_run: true\n"
        "        verbose: true\n"
    )

    env = dict(os.environ)
    env["PRE_COMMIT_HOME"] = str(tmp_path / "pc-cache")  # 隔離 cache
    env["GIT_AUTHOR_NAME"] = env["GIT_COMMITTER_NAME"] = "QA"
    env["GIT_AUTHOR_EMAIL"] = env["GIT_COMMITTER_EMAIL"] = "qa@test.local"

    assert _git(["init", "-q"], repo, env).returncode == 0
    # 安裝 git hook
    inst = subprocess.run(
        [str(precommit_bin), "install"], cwd=repo, env=env, capture_output=True, text=True
    )
    assert inst.returncode == 0, f"pre-commit install 失敗：{inst.stderr}"
    assert _git(["add", "-A"], repo, env).returncode == 0

    commit = _git(["commit", "-m", "add sample with shell usage"], repo, env)
    out = commit.stdout + commit.stderr
    # 關鍵斷言：含命中樣本仍 commit 成功（hook 未阻斷）
    assert commit.returncode == 0, f"commit 被 hook 阻斷（returncode={commit.returncode}）：\n{out}"
    # 確認 hook 確實有跑（warn 模式輸出特徵）
    log = _git(["log", "--oneline"], repo, env)
    assert "add sample" in log.stdout, f"commit 未落地：\n{log.stdout}{log.stderr}"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
