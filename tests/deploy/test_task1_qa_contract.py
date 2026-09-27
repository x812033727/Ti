"""QA contract tests for task #1 deploy follow-ups."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from studio import autopilot


def _decorator_has_autouse_true(decorator: ast.expr) -> bool:
    return (
        isinstance(decorator, ast.Call)
        and isinstance(decorator.func, ast.Attribute)
        and decorator.func.attr == "fixture"
        and any(
            keyword.arg == "autouse"
            and isinstance(keyword.value, ast.Constant)
            and keyword.value.value is True
            for keyword in decorator.keywords
        )
    )


def _fixture_patches_import_smoke(path: Path) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        if not any(_decorator_has_autouse_true(decorator) for decorator in node.decorator_list):
            continue
        for child in ast.walk(node):
            if not (
                isinstance(child, ast.Call)
                and isinstance(child.func, ast.Attribute)
                and child.func.attr == "setattr"
                and len(child.args) >= 2
            ):
                continue
            target, name = child.args[0], child.args[1]
            if (
                isinstance(target, ast.Name)
                and target.id == "redeploy"
                and isinstance(name, ast.Constant)
                and name.value == "import_smoke"
            ):
                return True
    return False


@pytest.mark.parametrize(
    "path",
    [
        Path("tests/deploy/test_redeploy_qa.py"),
        Path("tests/deploy/test_web_redeploy_qa.py"),
    ],
)
def test_redeploy_endpoint_tests_stub_import_smoke(path: Path):
    assert _fixture_patches_import_smoke(path), f"{path} must stub redeploy.import_smoke"


@pytest.mark.asyncio
async def test_prepare_clone_force_refspec_survives_git_credential_argv(tmp_path, monkeypatch):
    branch = "release/qa"
    work = tmp_path / "work"
    (work / ".git").mkdir(parents=True)
    calls: list[list[str]] = []

    monkeypatch.setattr(autopilot.config, "AUTOPILOT_BRANCH", branch)
    monkeypatch.setattr(autopilot.config, "AUTOPILOT_WORK_DIR", str(work))
    monkeypatch.setattr(
        autopilot,
        "_git_cred_argv",
        lambda: ["-c", "http.https://github.com/.extraheader=AUTHORIZATION: basic token"],
    )
    monkeypatch.setattr(autopilot, "_git_cred_env", lambda: None)

    async def fake_run(cmd, **_kwargs):
        calls.append(cmd)
        return 0, ""

    monkeypatch.setattr(autopilot, "_run", fake_run)

    assert await autopilot._prepare_clone() == str(work)

    fetches = [cmd for cmd in calls if "fetch" in cmd]
    assert len(fetches) == 1
    fetch = fetches[0]
    fetch_index = fetch.index("fetch")
    assert fetch[fetch_index + 1 :] == [
        "origin",
        f"+refs/heads/{branch}:refs/remotes/origin/{branch}",
    ]
    assert branch not in fetch[fetch_index + 1 :]
