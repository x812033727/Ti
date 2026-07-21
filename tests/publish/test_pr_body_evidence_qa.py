"""QA for task #2: PR body must explain why the change exists and how it was verified."""

from __future__ import annotations

import re

from scripts import qa_check_pr_body_evidence
from studio import publisher


def _section(body: str, heading: str) -> str:
    match = re.search(rf"^## {re.escape(heading)}\n(?P<section>.*?)(?=^## |\Z)", body, re.M | re.S)
    assert match, f"missing section: {heading}"
    return match.group("section").strip()


def test_pr_body_has_non_empty_motivation_and_test_verification_for_behavior_change():
    payload = publisher.pr_payload(
        "修正 webhook 逾時邊界",
        "ti-studio/task-2",
        "main",
        changed_files=[
            "studio/notify.py",
            "tests/autopilot/test_notify_webhook.py",
            "tests/autopilot/test_notify_config.py",
        ],
    )

    motivation = _section(payload["body"], "動機")
    verification = _section(payload["body"], "如何驗證")

    assert "類型：未處理邊界" in motivation
    assert "修正 webhook 逾時邊界" in motivation
    assert "未提供原始需求" not in motivation
    assert "對應測試：" in verification
    assert "`tests/autopilot/test_notify_webhook.py`" in verification
    assert "`tests/autopilot/test_notify_config.py`" in verification
    assert "靜態推理依據" not in verification


def test_pr_body_uses_static_reasoning_only_when_no_tests_are_changed():
    payload = publisher.pr_payload(
        "修正 README 過時文件",
        "ti-studio/task-2-docs",
        "main",
        changed_files=["README.md", "docs/publish.md"],
    )

    motivation = _section(payload["body"], "動機")
    verification = _section(payload["body"], "如何驗證")

    assert "類型：過時文件" in motivation
    assert "修正 README 過時文件" in motivation
    assert "靜態推理依據：" in verification
    assert "`README.md`" in verification
    assert "`docs/publish.md`" in verification
    assert "對應測試：" not in verification


def test_pr_body_empty_requirement_is_explicitly_marked_not_silent():
    payload = publisher.pr_payload(
        "",
        "ti-studio/task-2-empty",
        "main",
        changed_files=["studio/publisher.py"],
    )

    motivation = _section(payload["body"], "動機")
    verification = _section(payload["body"], "如何驗證")

    assert "類型：未明" in motivation
    assert "未提供原始需求" in motivation
    assert "bug、邊界、錯字或過時資訊" in motivation
    assert "靜態推理依據：" in verification
    assert "`studio/publisher.py`" in verification


def test_live_checker_accepts_pr_body_with_motivation_and_test_evidence():
    body = (
        "## 動機\n"
        "- 類型：bug\n"
        "- 說明：修正 PR 描述缺少審查依據的錯誤。\n\n"
        "## 如何驗證\n"
        "- 對應測試：`tests/publish/test_pr_body_evidence_qa.py`\n"
    )

    assert qa_check_pr_body_evidence.check_body(body) == []


def test_live_checker_rejects_missing_evidence_sections():
    body = "## 變更\n- 只有變更摘要，沒有原因或驗證方式。\n"

    assert qa_check_pr_body_evidence.check_body(body) == [
        "缺少 `## 動機` 區塊",
        "缺少 `## 如何驗證` 區塊",
    ]


def test_live_checker_rejects_vague_motivation_and_verification():
    body = "## 動機\n- 說明：整理內容。\n\n## 如何驗證\n- 看起來正常。\n"

    assert qa_check_pr_body_evidence.check_body(body) == [
        "`## 動機` 未說明 bug／邊界／錯字／過時等具體原因",
        "`## 如何驗證` 未指出對應測試或靜態推理依據",
    ]
