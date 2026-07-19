"""守護測試：autoformat 寫回的 diff 經 _commit_push_merge 不掉檔。

紅樣本已驗過判別力：曾暫把 _commit_push_merge 的 staging 改成只 add README.md，本測試會因
pkg/formatted.py 未進 HEAD commit 而紅；恢復實作後轉綠。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from studio import autopilot, config

_TASK = {"id": "249-writeback", "title": "autoformat writeback commit guard", "detail": ""}


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"git {' '.join(args)} failed in {cwd}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    return result.stdout


def _clone_with_origin_main(tmp_path: Path) -> Path:
    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    clone = tmp_path / "clone"

    _git(tmp_path, "init", "--bare", "--initial-branch=main", str(origin))
    _git(tmp_path, "clone", str(origin), str(seed))
    _git(seed, "config", "user.email", "seed@example.test")
    _git(seed, "config", "user.name", "Seed")

    (seed / "pkg").mkdir()
    (seed / "README.md").write_text("seed\n", encoding="utf-8")
    (seed / "pkg" / "formatted.py").write_text("numbers=[1,2,3]\n", encoding="utf-8")
    _git(seed, "add", "README.md", "pkg/formatted.py")
    _git(seed, "commit", "-m", "seed")
    _git(seed, "push", "-u", "origin", "main")
    _git(tmp_path, "clone", "--branch", "main", str(origin), str(clone))
    return clone


@pytest.mark.asyncio
async def test_autoformat_writeback_diff_is_committed_via_commit_push_merge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clone = _clone_with_origin_main(tmp_path)
    formatted = clone / "pkg" / "formatted.py"
    writeback = "numbers = [1, 2, 3]\n"
    formatted.write_text(writeback, encoding="utf-8")
    assert _git(clone, "diff", "--name-only").splitlines() == ["pkg/formatted.py"]

    monkeypatch.setattr(config, "AUTOPILOT_REPO", "core/autopilot")
    monkeypatch.setattr(config, "AUTOPILOT_BRANCH", "main")
    monkeypatch.setattr(config, "AUTOPILOT_DRYRUN", True)
    monkeypatch.setattr(config, "AUTOPILOT_FORCE_PUSH", False)
    monkeypatch.setattr(config, "AUTOPILOT_PROTECTION_CHECK", False)
    monkeypatch.setattr(config, "PUBLISH_REPO", "")
    monkeypatch.setattr(config, "PUBLISH_OWNER_ALLOWLIST", frozenset({"core"}))

    ok, msg = await autopilot._commit_push_merge(str(clone), _TASK)

    assert ok is True, msg
    assert "[dryrun]" in msg
    changed = set(
        _git(clone, "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD").splitlines()
    )
    assert "pkg/formatted.py" in changed
    assert _git(clone, "show", "HEAD:pkg/formatted.py") == writeback
    assert _git(clone, "status", "--porcelain") == ""
