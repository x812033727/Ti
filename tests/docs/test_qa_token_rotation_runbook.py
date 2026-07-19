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


def _section(text: str, heading: str) -> str:
    match = re.search(rf"^## .*{re.escape(heading)}.*$", text, re.MULTILINE)
    assert match, f"缺少章節: {heading}"
    start = match.start()
    next_heading = re.search(r"^## ", text[match.end() :], re.MULTILINE)
    if next_heading is None:
        return text[start:]
    return text[start : match.end() + next_heading.start()]


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
    dod = _section(text, "輪替驗證 DoD")

    # 必須明確綁定新 token（防止 keyring 舊 token 假綠）
    assert (
        'GH_TOKEN="$GH_PAT"' in dod
    ), 'DoD 缺 GH_TOKEN="$GH_PAT" gh auth status；裸跑 gh auth status 只驗 keyring 舊 token'
    assert "gh auth status" in dod

    # curl fallback：帶 Bearer header 打 /user
    assert "Authorization: Bearer $GH_PAT" in dod
    assert "https://api.github.com/user" in dod

    # HTTP 200 生效判定
    assert "200" in dod, "DoD 必須說明 HTTP 200 為生效判定"

    # scope 警告：200 不等於有 Contents RW 權限
    assert (
        "scope" in dod or "Contents: Read and write" in dod
    ), "DoD 缺 scope != 200 說明（token 有效不代表有 Contents RW 權限，scope 錯仍 403）"

    # curl 洩漏面：真正外洩點是 process argv，非 shell history 字面
    assert (
        "process argv" in dod or "ps aux" in dod
    ), "DoD 缺 curl token 洩漏面說明（真正外洩點是 process argv，非 history 字面）"

    # 斷鏈訊號：到期/撤銷在 gh release create 以 403 失敗
    assert "gh release create" in dod
    assert "403" in dod
    assert re.search(r"到期|撤銷", dod), "DoD 必須說明 token 到期/撤銷的失敗情境"


def test_runbook_contains_no_bare_python_command() -> None:
    text = _text()

    bare_python = re.search(r"(^|[\s`$])python\s", text)
    assert bare_python is None, "runbook 不得出現裸 `python` 指令；請用 python3 或 .venv/bin/python"
