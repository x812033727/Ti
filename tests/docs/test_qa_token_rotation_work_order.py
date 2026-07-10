"""QA guard tests for the 2026-07-10 `GH_PAT` rotation work order."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RUNBOOK = ROOT / "docs" / "token-rotation-runbook.md"
WORK_ORDER = ROOT / "docs" / "evidence" / "token-rotation-2026-07-10.md"

TOKEN_SECRET_RE = re.compile(r"(?:ghp|gho|ghs|ghr)_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{20,}")


def _text(path: Path) -> str:
    assert path.exists(), f"缺少必要文件: {path.relative_to(ROOT)}"
    return path.read_text(encoding="utf-8")


def _index(text: str, needle: str) -> int:
    pos = text.find(needle)
    assert pos >= 0, f"工作單缺少必要文字: {needle}"
    return pos


def _section(text: str, heading: str) -> str:
    match = re.search(rf"^##+ .*{re.escape(heading)}.*$", text, re.MULTILINE)
    assert match, f"缺少章節: {heading}"
    start = match.start()
    next_heading = re.search(r"^##+ ", text[match.end() :], re.MULTILINE)
    if next_heading is None:
        return text[start:]
    return text[start : match.end() + next_heading.start()]


def test_prerequisite_runbook_and_work_order_exist() -> None:
    runbook = _text(RUNBOOK)
    work_order = _text(WORK_ORDER)

    assert "三步驟主線" in runbook
    assert "先發後撤" in runbook
    assert "可勾選執行清單" in work_order
    assert "docs/token-rotation-runbook.md" in work_order


def test_work_order_locks_safe_issue_update_verify_revoke_order() -> None:
    text = _text(WORK_ORDER)

    assert "發新 → 更新 `.env` 與同名 repo secret 兩處 → 驗證 → 撤舊" in text
    assert "先撤後發" not in text
    assert "GITHUB_TOKEN" not in text, "工作單不得把輪替對象誤寫成內建 GITHUB_TOKEN"

    issue_pos = _index(text, "## 步驟 1")
    update_pos = _index(text, "## 步驟 2a")
    verify_pos = _index(text, "## 步驟 2b")
    revoke_pos = _index(text, "## 步驟 3")
    scan_pos = _index(text, "## 收尾")
    assert issue_pos < update_pos < verify_pos < revoke_pos < scan_pos

    pre_revoke = text[:revoke_pos]
    assert "該舊 token → Delete" not in pre_revoke
    assert "到 UI 撤舊" not in pre_revoke


def test_each_executable_step_has_owner_and_checklist_items() -> None:
    text = _text(WORK_ORDER)

    expected = {
        "步驟 1": "【人工】",
        "步驟 2a": "【人工】",
        "步驟 2b": "【AI 可代勞】",
        "步驟 3": "【人工】",
        "收尾": "【AI 可代勞】",
    }
    for heading, owner in expected.items():
        section = _section(text, heading)
        assert owner in section, f"{heading} 未標明 {owner}"
        assert "- [ ]" in section, f"{heading} 缺少可勾選清單"

    assert "明文不進對話/版控/工具輸出" in text
    assert "AI 不得代行" in _section(text, "步驟 1")
    assert "AI 不得代為撤銷" in _section(text, "步驟 3")


def test_evidence_fields_exist_for_verify_scan_and_report() -> None:
    text = _text(WORK_ORDER)

    for heading in [
        "貼證欄位：步驟 2b 驗證輸出",
        "貼證欄位：`--scan` 掃描輸出",
        "貼證欄位：`--report` 人工/AI 分界狀態表",
    ]:
        section = _section(text, heading)
        assert "```" in section, f"{heading} 缺少可貼實跑輸出的 fenced block"
        assert "待" in section, f"{heading} 應明確標示待回填或待人工"

    assert "步驟 1（發新）與步驟 3（撤舊）待**人工**於 GitHub UI 完成" in text


def test_no_plaintext_token_secret_is_committed_in_work_order() -> None:
    text = _text(WORK_ORDER)

    matches = TOKEN_SECRET_RE.findall(text)
    assert not matches, f"工作單疑似含 GitHub token 明文: {matches}"
