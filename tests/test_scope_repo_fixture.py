from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _git(repo: Path, *args: str) -> str:
    cp = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        check=True,
        text=True,
    )
    return cp.stdout.strip()


def test_scope_repo_stays_in_tmp_path_and_supports_layered_commits(
    scope_repo,
    tmp_path: Path,
):
    root = scope_repo.path.resolve()

    assert root.parent == tmp_path.resolve()
    assert root != REPO_ROOT
    assert REPO_ROOT not in root.parents
    assert (root / ".git").is_dir()

    assert scope_repo.baseline
    assert (
        _git(root, "rev-parse", "--verify", f"{scope_repo.baseline}^{{commit}}")
        == scope_repo.baseline
    )

    scope_repo.write("alpha.txt", "one\n")
    first = scope_repo.commit("add alpha")

    scope_repo.write("alpha.txt", "two\n")
    scope_repo.write("nested/beta.py", "print('beta')\n")
    second = scope_repo.commit("update alpha and add beta")

    assert first != second
    assert _git(root, "rev-list", "--count", f"{scope_repo.baseline}..HEAD") == "2"
    assert _git(root, "diff", "--name-only", f"{scope_repo.baseline}..HEAD").splitlines() == [
        "alpha.txt",
        "nested/beta.py",
    ]
