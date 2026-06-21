"""LLM 核心穩定公開契約文件守護測試。"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / "llm-caller-public-contract.md"


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_contract_doc_exists_and_covers_required_topics():
    text = DOC.read_text(encoding="utf-8")

    for required in (
        "公開介面",
        "429 vs 529",
        "SDK `max_retries` 關閉約定",
        "CORE_REPO 路由",
        "RetryConfig",
        "run_with_retries",
        "classify_failure",
        "max_retries=0",
        "config.CORE_REPO",
        "核心改動:",
    ):
        assert required in text


def test_contract_doc_anchors_match_source():
    llm = _read("studio/llm_caller.py")
    providers = _read("studio/providers.py")
    config = _read("studio/config.py")
    arch = _read("ARCHITECTURE.md")

    for anchor in (
        "class RetryConfig",
        "def run_with_retries",
        "def classify_failure",
        "class RateLimitSignal",
        "class OverloadedSignal",
        "class APIErrorSignal",
    ):
        assert anchor in llm

    assert "max_retries=0" in providers
    assert "CORE_REPO = AUTOPILOT_REPO" in config
    assert "docs/llm-caller-public-contract.md" in arch
