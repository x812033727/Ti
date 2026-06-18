"""QA 任務 #1：守護 deploy-test CI gate 與 README required check 文件。

重點不是重跑部署邏輯，而是防止 merge gate 被悄悄改弱：
固定 job 名稱、固定 pytest 目錄指令、不吞錯、不加 coverage、不用 path filter。
"""

from __future__ import annotations

import re

import pytest
from _repo import REPO_ROOT

yaml = pytest.importorskip("yaml")

CI = REPO_ROOT / ".github" / "workflows" / "ci.yml"
README = REPO_ROOT / "README.md"


def _ci_text() -> str:
    return CI.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def doc():
    return yaml.safe_load(_ci_text())


@pytest.fixture(scope="module")
def deploy_job(doc):
    return doc["jobs"]["deploy-test"]


def _job_block(job_name: str) -> str:
    lines = _ci_text().splitlines()
    start = next(i for i, line in enumerate(lines) if line == f"  {job_name}:")
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if re.match(r"^  [A-Za-z0-9_-]+:\s*$", lines[i]):
            end = i
            break
    return "\n".join(lines[start:end])


def _step_by_name(job: dict, step_name: str) -> dict:
    matches = [step for step in job["steps"] if step.get("name") == step_name]
    assert len(matches) == 1, f"預期剛好一個 step: {step_name!r}，實際 {len(matches)} 個"
    return matches[0]


def test_deploy_test_job_has_fixed_id_and_display_name(doc, deploy_job):
    assert "deploy-test" in doc["jobs"], "ci.yml 缺固定 job id: deploy-test"
    assert deploy_job.get("name") == "deploy-test", "required check 顯示名稱不可漂移"


def test_deploy_test_uses_python_312_and_short_timeout(deploy_job):
    setup = _step_by_name(deploy_job, "Set up Python")
    assert setup["with"]["python-version"] == "3.12"
    assert deploy_job["timeout-minutes"] <= 10


def test_deploy_test_runs_exact_pytest_command_with_sandbox_off(deploy_job):
    run = _step_by_name(deploy_job, "Run deploy tests")
    assert run["run"] == "python -m pytest tests/deploy -q"
    assert run.get("env", {}).get("TI_SANDBOX") == "0"


def test_deploy_test_does_not_hide_or_water_down_failures(deploy_job):
    block = _job_block("deploy-test")
    run_step = _step_by_name(deploy_job, "Run deploy tests")
    assert "continue-on-error" not in block
    assert "--cov" not in run_step["run"]
    assert "|| true" not in run_step["run"]


def test_workflow_and_deploy_job_have_no_path_filter():
    text = _ci_text()
    on_section = text.split("\njobs:", 1)[0]
    deploy_block = _job_block("deploy-test")
    for section_name, section in (("on", on_section), ("deploy-test", deploy_block)):
        assert not re.search(r"^\s+paths(?:-ignore)?:", section, re.MULTILINE), (
            f"{section_name} 不可用 paths / paths-ignore 過濾，避免 required check 變 pending 或消失"
        )


def test_readme_documents_deploy_test_as_required_check():
    readme = README.read_text(encoding="utf-8")
    required_lines = [
        line
        for line in readme.splitlines()
        if "required check" in line.lower() or "required checks" in line.lower()
    ]
    assert required_lines, "README 缺 required checks 說明"
    text = "\n".join(required_lines)
    assert "`deploy-test`" in text
    assert re.search(r"branch protection|ruleset", text, re.IGNORECASE), (
        "README 必須明確提醒在 branch protection / ruleset 設為 required"
    )
