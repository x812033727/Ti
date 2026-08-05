"""QA 驗收：任務 #1「redeploy QA 成功路徑不得真跑 import smoke 子程序」。

驗收重點：
- redeploy 相關 QA 檔的 autouse fixture 預設 mock 掉 `redeploy.import_smoke`。
- direct `redeploy.redeploy()` 與 `/api/redeploy` 成功路徑若掉回真 `runner.run_command_exec`
  的 import-smoke 子程序，測試必須立刻失敗。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from studio import config, redeploy, runner

ROOT = config.PROJECT_ROOT


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _is_pytest_autouse_fixture(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    for decorator in node.decorator_list:
        if not isinstance(decorator, ast.Call):
            continue
        func = decorator.func
        if (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id == "pytest"
            and func.attr == "fixture"
        ):
            return any(
                kw.arg == "autouse"
                and isinstance(kw.value, ast.Constant)
                and kw.value.value is True
                for kw in decorator.keywords
            )
    return False


def _calls_monkeypatch_import_smoke(node: ast.AST) -> bool:
    for call in (n for n in ast.walk(node) if isinstance(n, ast.Call)):
        func = call.func
        if not (
            isinstance(func, ast.Attribute)
            and func.attr == "setattr"
            and isinstance(func.value, ast.Name)
            and func.value.id == "monkeypatch"
        ):
            continue
        if len(call.args) < 3:
            continue
        target, attr = call.args[0], call.args[1]
        if (
            isinstance(target, ast.Name)
            and target.id == "redeploy"
            and isinstance(attr, ast.Constant)
            and attr.value == "import_smoke"
        ):
            return True
    return False


@pytest.mark.parametrize(
    "path",
    [
        ROOT / "tests/deploy/test_redeploy_qa.py",
        ROOT / "tests/deploy/test_web_redeploy_qa.py",
    ],
)
def test_redeploy_qa_files_autouse_mock_import_smoke(path: Path):
    tree = _tree(path)
    fixtures = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and _is_pytest_autouse_fixture(node)
    ]
    assert fixtures, f"{path.name} 沒有 autouse fixture，成功路徑可能漏跑隔離設定"
    assert any(_calls_monkeypatch_import_smoke(node) for node in fixtures), (
        f"{path.name} 的 autouse fixture 未 mock redeploy.import_smoke，"
        "redeploy 成功路徑會掉回真 python -c import smoke"
    )


@pytest.mark.asyncio
async def test_direct_redeploy_uses_mocked_import_smoke_not_runner(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "AUTOPILOT_STATE_DIR", tmp_path)
    monkeypatch.setattr(redeploy.autonomy, "policy_exists", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(redeploy, "schedule_restart", lambda *a, **k: None)

    async def fake_pull():
        return runner.RunOutput("git pull", 0, "Already up to date.", False)

    smoke_calls = {"n": 0}

    async def fake_smoke():
        smoke_calls["n"] += 1
        return runner.RunOutput("import smoke", 0, "mocked", False)

    async def fail_if_runner_used(*_args, **_kwargs):
        raise AssertionError("import smoke must be mocked; runner.run_command_exec was called")

    monkeypatch.setattr(redeploy, "pull_main", fake_pull)
    monkeypatch.setattr(redeploy, "import_smoke", fake_smoke)
    monkeypatch.setattr(runner, "run_command_exec", fail_if_runner_used)

    result = await redeploy.redeploy()

    assert result["ok"] is True
    assert result["restarting"] is True
    assert smoke_calls["n"] == 1


def test_endpoint_redeploy_uses_mocked_import_smoke_not_runner(monkeypatch, tmp_path):
    from studio.server import app

    monkeypatch.setattr(config, "ACCESS_PASSWORD", "")
    monkeypatch.setattr(config, "AUTOPILOT_STATE_DIR", tmp_path)
    monkeypatch.setattr(redeploy.autonomy, "policy_exists", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(redeploy, "schedule_restart", lambda *a, **k: None)

    async def fake_pull():
        return runner.RunOutput("git pull", 0, "Already up to date.", False)

    smoke_calls = {"n": 0}

    async def fake_smoke():
        smoke_calls["n"] += 1
        return runner.RunOutput("import smoke", 0, "mocked", False)

    async def fail_if_runner_used(*_args, **_kwargs):
        raise AssertionError("endpoint success path must use mocked import_smoke")

    monkeypatch.setattr(redeploy, "pull_main", fake_pull)
    monkeypatch.setattr(redeploy, "import_smoke", fake_smoke)
    monkeypatch.setattr(runner, "run_command_exec", fail_if_runner_used)

    client = TestClient(app, client=("127.0.0.1", 12345))
    body = client.post("/api/redeploy").json()

    assert body["ok"] is True
    assert body["restarting"] is True
    assert smoke_calls["n"] == 1
