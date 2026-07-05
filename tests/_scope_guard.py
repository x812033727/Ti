from __future__ import annotations

import subprocess
from collections.abc import Iterable
from fnmatch import fnmatch
from pathlib import Path


def find_scope_violations(
    changed: Iterable[str], allowed_globs: Iterable[str] = ()
) -> list[str]:
    """Return changed Python files that are outside the allowed path globs."""
    allowed = tuple(allowed_globs)
    return sorted(
        {
            path
            for path in changed
            if path.endswith(".py") and not any(fnmatch(path, glob) for glob in allowed)
        }
    )


def collect_changed_files(repo: str | Path, baseline_ref: str) -> list[str]:
    root = Path(repo)
    diff = subprocess.run(
        ["git", "diff", "--name-only", f"{baseline_ref}..HEAD"],
        cwd=root,
        capture_output=True,
        check=True,
        text=True,
    )
    untracked = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard"],
        cwd=root,
        capture_output=True,
        check=True,
        text=True,
    )
    paths = diff.stdout.splitlines() + untracked.stdout.splitlines()
    return sorted({path for path in paths if path.strip()})


def find_repo_scope_violations(
    repo: str | Path, baseline_ref: str, allowed_globs: Iterable[str] = ()
) -> list[str]:
    changed = collect_changed_files(repo, baseline_ref)
    return find_scope_violations(changed, allowed_globs)
