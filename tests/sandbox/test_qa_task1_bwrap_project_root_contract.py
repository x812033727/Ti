"""QA task #1: project-root mounts in the bwrap prefix.

These tests are intentionally static. They catch the mount-order regression even
on machines where bubblewrap is unavailable, while the neighboring sandbox tests
cover the real bwrap execution path when the binary exists.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from studio import runner


def _triples(args: list[str]) -> list[tuple[str, str, str]]:
    return [tuple(args[i : i + 3]) for i in range(len(args) - 2)]


def _triple_index(args: list[str], triple: tuple[str, str, str]) -> int:
    triples = _triples(args)
    assert triple in triples, f"missing {triple!r} in {args!r}"
    return triples.index(triple)


def test_tmp_project_root_is_rebound_readonly_after_tmpfs(tmp_path, monkeypatch):
    project_root = tmp_path / "repo"
    cwd = tmp_path / "lane"
    project_root.mkdir()
    cwd.mkdir()
    monkeypatch.setattr(runner.config, "PROJECT_ROOT", project_root)

    args = runner._bwrap_prefix(cwd)
    root = str(project_root.resolve())
    cwd_s = str(cwd.resolve())

    tmpfs_index = args.index("/tmp")
    cwd_bind_index = _triple_index(args, ("--bind", cwd_s, cwd_s))
    project_ro_index = _triple_index(args, ("--ro-bind", root, root))
    chdir_index = args.index("--chdir")

    assert tmpfs_index < cwd_bind_index < project_ro_index < chdir_index


@pytest.mark.parametrize("cwd_kind", ["project-root", "project-child"])
def test_project_root_parent_does_not_cover_writable_cwd(tmp_path, monkeypatch, cwd_kind):
    project_root = tmp_path / "repo"
    cwd = project_root if cwd_kind == "project-root" else project_root / "lane"
    cwd.mkdir(parents=True)
    monkeypatch.setattr(runner.config, "PROJECT_ROOT", project_root)

    args = runner._bwrap_prefix(cwd)
    root = str(project_root.resolve())
    cwd_s = str(Path(cwd).resolve())

    assert ("--ro-bind", root, root) not in _triples(args)
    assert ("--bind", cwd_s, cwd_s) in _triples(args)


def test_project_root_inside_writable_cwd_avoids_overlapping_mount(tmp_path, monkeypatch):
    cwd = tmp_path / "workspace"
    project_root = cwd / "repo"
    project_root.mkdir(parents=True)
    monkeypatch.setattr(runner.config, "PROJECT_ROOT", project_root)

    args = runner._bwrap_prefix(cwd)
    root = str(project_root.resolve())

    assert ("--ro-bind", root, root) not in _triples(args)
