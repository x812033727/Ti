"""QA guards for the GH_PAT token rotation work order."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = ROOT / "docs" / "evidence" / "token-rotation-2026-07-10.md"


def _text() -> str:
    assert EVIDENCE.exists(), "缺少 docs/evidence/token-rotation-2026-07-10.md"
    return EVIDENCE.read_text(encoding="utf-8")


def test_token_rotation_work_order_exists_and_locks_safe_order() -> None:
    text = _text()

    assert "發新 -> 更新 repo secret 與 `.env` -> 驗證 -> 撤舊" in text
    assert "新 token 未驗證通過前，不得撤銷舊 token" in text
    assert "1. 發新 fine-grained PAT | 人工 | 待人工" in text
    assert "3. 撤銷舊 token | 人工 | 待人工" in text


def test_token_rotation_work_order_documents_ai_boundary_and_commands() -> None:
    text = _text()

    assert "AI 可代勞" in text
    assert "bash scripts/verify_token_rotation.sh --verify" in text
    assert "bash scripts/verify_token_rotation.sh --scan" in text
    assert 'GH_TOKEN="$GH_PAT" gh auth status' in text
    assert "curl fallback 的 HTTP 200 只證明身分有效，不證 repository scope" in text
    assert "不記錄、不貼上 token 明文" in text
