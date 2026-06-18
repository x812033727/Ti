"""QA 驗收：Ruff pre-commit 自動修正時必須阻斷提交流程。

驗證面向：
  - pre-commit Ruff hook 保留既有最小設定 args: [--fix]
  - 實跑原始 hook 設定：git commit 在 hook 修檔後會被 pre-commit 阻擋
  - Ruff 版本在 pyproject / pre-commit / CI 三端一致
  - 文件明確告知「修正後會停止，需重新 stage」
  - 實跑 git commit：第一次修檔失敗，重新 stage 後第二次通過
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import textwrap
from pathlib import Path

import pytest
import tomllib
from _repo import REPO_ROOT

yaml = pytest.importorskip("yaml")

REPO = REPO_ROOT
PRECOMMIT = REPO / ".pre-commit-config.yaml"
PYPROJECT = REPO / "pyproject.toml"
CI_YML = REPO / ".github" / "workflows" / "ci.yml"
CONTRIBUTING = REPO / "CONTRIBUTING.md"


def _precommit_data() -> dict:
    return yaml.safe_load(PRECOMMIT.read_text(encoding="utf-8"))


def _ruff_repo_and_hook() -> tuple[dict, dict]:
    data = _precommit_data()
    for repo in data["repos"]:
        if repo.get("repo") == "https://github.com/astral-sh/ruff-pre-commit":
            for hook in repo.get("hooks", []):
                if hook.get("id") == "ruff":
                    return repo, hook
    raise AssertionError("pre-commit 找不到 ruff-pre-commit 的 ruff hook")


def _dev_ruff_version() -> str:
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    deps = data["project"]["optional-dependencies"]["dev"]
    for dep in deps:
        match = re.fullmatch(r"ruff==([0-9.]+)", dep)
        if match:
            return match.group(1)
    raise AssertionError("pyproject [dev] 找不到精確 ruff==x.y.z pin")


def _run(cmd: list[str], cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=60)


def _workspace_tempdir():
    return tempfile.TemporaryDirectory(prefix=".qa-ruff-precommit-", dir=REPO)


def _fixable_source() -> str:
    return "import sys\n\nprint('kept')\n"


def test_ruff_hook_keeps_minimal_autofix_args():
    _repo, hook = _ruff_repo_and_hook()
    args = hook.get("args", [])

    assert args == ["--fix"], (
        "實測證明 pre-commit 會在 hook 修檔後阻擋 git commit；"
        f"Ruff hook 應保留既有最小設定，實得 args={args}"
    )


def test_ruff_version_is_aligned_across_dev_precommit_and_ci():
    repo, _hook = _ruff_repo_and_hook()
    version = _dev_ruff_version()
    ci_text = CI_YML.read_text(encoding="utf-8")

    assert repo.get("rev") == f"v{version}", (
        f".pre-commit-config.yaml ruff rev 與 pyproject dev 版本不一致："
        f"{repo.get('rev')} vs ruff=={version}"
    )
    assert f"ruff=={version}" in ci_text, f"CI 未安裝與 pyproject 相同的 Ruff 版本 {version}"


def test_contributing_documents_fix_then_restage_flow():
    text = CONTRIBUTING.read_text(encoding="utf-8")
    paragraph = "\n".join(
        line for line in text.splitlines() if "pre-commit" in line or "Ruff hook" in line
    )

    assert "自動修正" in paragraph, f"CONTRIBUTING 未說明 Ruff hook 會自動修正：\n{paragraph}"
    assert "提交會停止" in paragraph, f"CONTRIBUTING 未說明修正後提交會停止：\n{paragraph}"
    assert "重新 stage" in paragraph, f"CONTRIBUTING 未提醒修正後需重新 stage：\n{paragraph}"


def test_plain_ruff_fix_would_silently_succeed_without_exit_flag():
    ruff_bin = REPO / ".venv" / "bin" / "ruff"
    if not ruff_bin.exists():
        pytest.skip("環境無 .venv/bin/ruff")

    with _workspace_tempdir() as raw:
        work = Path(raw)
        target = work / "bad.py"
        target.write_text(_fixable_source(), encoding="utf-8")

        env = dict(os.environ)
        cp = _run([str(ruff_bin), "check", "--fix", str(target)], work, env)
        out = cp.stdout + cp.stderr

        assert "import sys" not in target.read_text(encoding="utf-8"), (
            f"前提失敗：ruff --fix 沒有修掉 F401：\n{out}"
        )
        assert cp.returncode == 0, (
            "對照組失敗：未加 --exit-non-zero-on-fix 時，ruff --fix 預期會在修檔後回 0；"
            f"實得 {cp.returncode}\n{out}"
        )


def test_original_fix_only_precommit_hook_blocks_commit_then_passes_after_restage():
    if shutil.which("git") is None:
        pytest.skip("環境無 git")
    precommit_bin = REPO / ".venv" / "bin" / "pre-commit"
    ruff_bin = REPO / ".venv" / "bin" / "ruff"
    if not precommit_bin.exists() or not ruff_bin.exists():
        pytest.skip("環境無 .venv/bin/pre-commit 或 .venv/bin/ruff")

    _repo, hook = _ruff_repo_and_hook()
    args = hook.get("args", [])

    with _workspace_tempdir() as raw:
        work = Path(raw)
        target = work / "bad.py"
        target.write_text(_fixable_source(), encoding="utf-8")
        (work / ".pre-commit-config.yaml").write_text(
            textwrap.dedent(
                f"""
                repos:
                  - repo: local
                    hooks:
                      - id: ruff
                        name: ruff
                        entry: ruff check
                        language: system
                        types_or: [python, pyi]
                        args: [{", ".join(args)}]
                """
            ).lstrip(),
            encoding="utf-8",
        )

        env = dict(os.environ)
        env["PATH"] = f"{REPO / '.venv' / 'bin'}{os.pathsep}{env.get('PATH', '')}"
        env["PRE_COMMIT_HOME"] = str(work / ".pre-commit-home")

        init = _run(["git", "init", "-q"], work, env)
        assert init.returncode == 0, init.stderr
        set_email = _run(["git", "config", "user.email", "qa@example.test"], work, env)
        assert set_email.returncode == 0, set_email.stderr
        set_name = _run(["git", "config", "user.name", "QA"], work, env)
        assert set_name.returncode == 0, set_name.stderr
        install = _run([str(precommit_bin), "install"], work, env)
        assert install.returncode == 0, install.stdout + install.stderr
        add = _run(["git", "add", "-A"], work, env)
        assert add.returncode == 0, add.stderr

        first = _run(["git", "commit", "-m", "probe"], work, env)
        first_out = first.stdout + first.stderr
        fixed_text = target.read_text(encoding="utf-8")

        assert "import sys" not in fixed_text, f"前提失敗：ruff hook 沒有修檔：\n{first_out}"
        assert first.returncode != 0, (
            "原始 args: [--fix] 的 Ruff hook 修檔後，git commit 應被 pre-commit 阻擋：\n"
            + first_out
        )
        assert (
            "files were modified by this hook" in first_out
            or "Fixed" in first_out
            or "fixed" in first_out
        ), f"第一次輸出看不到修檔訊號：\n{first_out}"

        restage = _run(["git", "add", "bad.py"], work, env)
        assert restage.returncode == 0, restage.stderr
        second = _run(["git", "commit", "-m", "probe"], work, env)
        second_out = second.stdout + second.stderr
        assert second.returncode == 0, (
            f"重新 stage 後第二次 commit 應乾淨通過，實得 {second.returncode}\n{second_out}"
        )
