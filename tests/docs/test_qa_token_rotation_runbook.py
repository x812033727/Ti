"""QA guard tests for the `GH_PAT` token rotation runbook."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RUNBOOK = ROOT / "docs" / "token-rotation-runbook.md"


def _text() -> str:
    assert RUNBOOK.exists(), "缺少 docs/token-rotation-runbook.md"
    return RUNBOOK.read_text(encoding="utf-8")


def _index(text: str, needle: str) -> int:
    pos = text.find(needle)
    assert pos >= 0, f"runbook 缺少必要文字: {needle}"
    return pos


def test_mainline_documents_safe_rotation_order_and_boundaries() -> None:
    text = _text()

    assert "先發後撤" in text
    assert "順序不可顛倒" in text
    assert "先發新、驗證可用、再撤舊" in text
    assert "人工 / AI 分界" in text

    issue_pos = _index(text, "### 步驟 1")
    update_pos = _index(text, "### 步驟 2")
    revoke_pos = _index(text, "### 步驟 3")
    assert issue_pos < update_pos < revoke_pos, "實際主線必須先發新、更新驗證、最後撤舊"

    step1 = text[issue_pos:update_pos]
    step2 = text[update_pos:revoke_pos]
    step3 = text[revoke_pos:]
    assert "發新 fine-grained PAT" in step1
    assert "人工" in step1
    assert "更新 `.env`" in step2
    assert "AI 可代勞" in step2
    assert "撤銷舊 token" in step3
    assert "人工" in step3


def test_fine_grained_pat_spec_is_locked_to_repo_contents_rw_and_gh_pat() -> None:
    text = _text()

    required_terms = [
        "Fine-grained",
        "Repository access",
        "只選本 repo",
        "Contents: Read and write",
        "GH_PAT",
        "到期日",
    ]
    missing = [term for term in required_terms if term not in text]
    assert not missing, f"發新 token 規格缺漏: {missing}"

    assert "不要用 classic PAT" in text
    assert "不可" in text and "All repositories" in text


def test_revocation_requires_manual_fine_grained_token_delete_path() -> None:
    text = _text()
    revoke_pos = _index(text, "### 步驟 3")
    revoke_text = text[revoke_pos:]

    assert "fine-grained PAT 無 API 可刪" in revoke_text
    assert "Settings → Developer settings" in revoke_text
    assert "Personal access tokens" in revoke_text
    assert "Fine-grained tokens" in revoke_text
    assert "Delete" in revoke_text
    assert "人工" in revoke_text


def test_residual_token_scan_section_has_primary_and_fallback_commands() -> None:
    text = _text()

    assert re.search(r"^## .*殘留 token 掃描", text, re.MULTILINE), "缺少殘留 token 掃描章節"
    assert "gitleaks detect --no-git" in text
    assert "grep -rnE" in text
    assert "history/*.jsonl" in text
    assert "session workspace" in text

    for prefix in ["ghp_", "github_pat_", "gho_", "ghs_", "ghr_"]:
        assert prefix in text, f"掃描 fallback 未涵蓋 GitHub token 前綴: {prefix}"


def test_rotation_dod_requires_live_auth_check_and_release_403_signal() -> None:
    text = _text()

    assert re.search(r"^## .*輪替驗證 DoD", text, re.MULTILINE), "缺少輪替驗證 DoD 章節"
    assert "gh auth status" in text or (
        'Authorization: Bearer $GH_PAT' in text and "https://api.github.com/user" in text
    )
    assert "200" in text
    assert "gh release create" in text
    assert "403" in text


def test_runbook_contains_no_bare_python_command() -> None:
    text = _text()

    bare_python = re.search(r"(^|[\s`$])python\s", text)
    assert bare_python is None, "runbook 不得出現裸 `python` 指令；請用 python3 或 .venv/bin/python"
