from __future__ import annotations

import os
from pathlib import Path
from typing import NamedTuple

import pytest

DEMO_ENV = "TI_SCOPE_FIXTURE_DEMO_ROOT"
REPO_ROOT = Path(__file__).resolve().parent.parent


class DemoWorkspace(NamedTuple):
    root: Path
    payload: Path


@pytest.fixture
def demo_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> DemoWorkspace:
    root = tmp_path / "fixture-demo"
    root.mkdir()
    payload = root / "payload.txt"
    payload.write_text("demo payload\n", encoding="utf-8")
    monkeypatch.setenv(DEMO_ENV, str(root))
    return DemoWorkspace(root=root, payload=payload)


def test_scoped_fixture_uses_tmp_path_and_monkeypatch(
    demo_workspace: DemoWorkspace, tmp_path: Path
) -> None:
    root = demo_workspace.root.resolve()
    assert root == tmp_path.resolve() / "fixture-demo"
    assert os.environ[DEMO_ENV] == str(demo_workspace.root)
    assert root != REPO_ROOT and REPO_ROOT not in root.parents
    assert demo_workspace.payload.read_text(encoding="utf-8") == "demo payload\n"


def test_scoped_fixture_env_does_not_leak_between_tests() -> None:
    assert DEMO_ENV not in os.environ
