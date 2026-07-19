"""任務 #1 QA：Ruff pre-commit 自動修正不可靜默通過。

驗收重點：
  - Ruff 版本在 pyproject / pre-commit / CI 三端一致
  - pre-commit 的 ruff hook 維持自動修正設定
  - 文件明確告知修檔後會停止，需重新 stage
  - 真實 git commit：第一次被 Ruff 修檔後失敗，重新 stage 後通過
  - @pytest.mark.realgit 僅供本機驗收；CI 因無 pre-commit 會自動 skip
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parents[2]
PRECOMMIT = REPO / ".pre-commit-config.yaml"
PYPROJECT = REPO / "pyproject.toml"
CI = REPO / ".github" / "workflows" / "ci.yml"
CONTRIBUTING = REPO / "CONTRIBUTING.md"
EVIDENCE_DOC = REPO / "docs" / "ruff-precommit-fix-gate.md"


def _load_precommit():
    return yaml.safe_load(PRECOMMIT.read_text(encoding="utf-8"))


def _ruff_precommit_repo():
    for repo in _load_precommit()["repos"]:
        if repo.get("repo") == "https://github.com/astral-sh/ruff-pre-commit":
            return repo
    raise AssertionError("pre-commit 缺 astral-sh/ruff-pre-commit repo")


def _ruff_hook():
    repo = _ruff_precommit_repo()
    for hook in repo.get("hooks", []):
        if hook.get("id") == "ruff":
            return hook
    raise AssertionError("pre-commit 缺 id=ruff hook")


def test_ruff_version_pin_consistent_across_dev_precommit_and_ci():
    pyproject = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    dev_deps = pyproject["project"]["optional-dependencies"]["dev"]
    pyproject_versions = {
        match.group(1) for dep in dev_deps if (match := re.fullmatch(r"ruff==([0-9.]+)", dep))
    }

    precommit_version = _ruff_precommit_repo()["rev"].removeprefix("v")
    ci_versions = set(re.findall(r"ruff==([0-9.]+)", CI.read_text(encoding="utf-8")))

    assert pyproject_versions == {
        precommit_version
    }, f"pyproject 與 pre-commit Ruff 版本不一致：{pyproject_versions} vs {precommit_version}"
    assert ci_versions == {
        precommit_version
    }, f"CI 與 pre-commit Ruff 版本不一致：{ci_versions} vs {precommit_version}"


def test_precommit_ruff_hook_keeps_auto_fix_without_exit_zero_escape():
    hook = _ruff_hook()
    args = hook.get("args", [])

    assert "--fix" in args, f"ruff hook 沒有自動修正參數：{hook}"
    assert "--exit-zero" not in args, f"ruff hook 不可用 --exit-zero 吃掉失敗：{hook}"


def test_docs_state_fixed_files_stop_commit_and_need_restaging():
    contributing = CONTRIBUTING.read_text(encoding="utf-8")
    evidence = EVIDENCE_DOC.read_text(encoding="utf-8")

    assert "pre-commit 偵測到檔案被修正時，提交會停止" in contributing
    assert "重新 stage 後再 commit" in contributing
    assert "docs/ruff-precommit-fix-gate.md" in contributing
    assert "exit_code=1" in evidence
    assert "files were modified by this hook" in evidence
    assert "不需要修改 hook" in evidence


def _run(args: list[str], *, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True, timeout=50)


def _git(args: list[str], *, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return _run(["git", *args], cwd=cwd, env=env)


def _precommit_bin() -> str | None:
    candidates = [
        REPO / ".venv" / "bin" / "pre-commit",
        Path(sys.executable).with_name("pre-commit"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return shutil.which("pre-commit")


@pytest.mark.realgit
def test_real_commit_fails_once_when_ruff_hook_autofixes_then_passes(tmp_path):
    """驗證 pre-commit commit gate；不驗證遠端 hook repo 下載流程。"""
    if shutil.which("git") is None:
        pytest.skip("環境無 git")
    precommit_bin = _precommit_bin()
    if precommit_bin is None:
        pytest.skip("環境無 pre-commit")

    repo = tmp_path / "repo"
    repo.mkdir()
    probe = repo / "qa_ruff_probe.py"
    probe.write_text("import os\n\nprint('ok')\n", encoding="utf-8")

    config = {
        "repos": [
            {
                "repo": "local",
                "hooks": [
                    {
                        "id": "ruff",
                        "name": "ruff",
                        "entry": f"{shlex.quote(sys.executable)} -m ruff check",
                        "language": "system",
                        "types": ["python"],
                        "args": _ruff_hook().get("args", []),
                    }
                ],
            }
        ]
    }
    (repo / ".pre-commit-config.yaml").write_text(
        yaml.safe_dump(config, sort_keys=False), encoding="utf-8"
    )

    env = dict(os.environ)
    env["PRE_COMMIT_HOME"] = str(tmp_path / "pre-commit-home")
    env["GIT_AUTHOR_NAME"] = env["GIT_COMMITTER_NAME"] = "QA"
    env["GIT_AUTHOR_EMAIL"] = env["GIT_COMMITTER_EMAIL"] = "qa@test.local"

    assert _git(["init", "-q"], cwd=repo, env=env).returncode == 0
    install = _run([precommit_bin, "install"], cwd=repo, env=env)
    assert install.returncode == 0, install.stdout + install.stderr
    assert _git(["add", "-A"], cwd=repo, env=env).returncode == 0

    first = _git(["commit", "-m", "probe"], cwd=repo, env=env)
    first_out = first.stdout + first.stderr

    assert first.returncode != 0, f"第一次 commit 不該通過：\n{first_out}"
    assert "files were modified by this hook" in first_out, first_out
    assert "import os" not in probe.read_text(encoding="utf-8"), "Ruff 未移除未使用 import"
    assert _git(["rev-parse", "--verify", "HEAD"], cwd=repo, env=env).returncode != 0

    assert _git(["add", "-A"], cwd=repo, env=env).returncode == 0
    second = _git(["commit", "-m", "probe fixed"], cwd=repo, env=env)
    second_out = second.stdout + second.stderr

    assert second.returncode == 0, f"重新 stage 後 commit 應通過：\n{second_out}"
    assert "ruff" in second_out and "Passed" in second_out


def test_no_probe_or_pc_cache_residue():
    for command in (
        ["git", "ls-files", ".pc-cache-qa", "qa_ruff_probe.py"],
        [
            "git",
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            "--",
            ".pc-cache-qa",
            "qa_ruff_probe.py",
        ],
    ):
        result = subprocess.run(
            command,
            cwd=REPO,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "", (
            "驗收後不可留下 tracked/untracked probe/cache residue："
            f"{command!r} -> {result.stdout!r}"
        )
