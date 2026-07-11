from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REPORT = ROOT / "docs" / "perf" / "prompt-cache-verification.md"


def _report_text() -> str:
    assert REPORT.exists(), "缺少 docs/perf/prompt-cache-verification.md"
    return REPORT.read_text(encoding="utf-8")


def _bash_blocks(text: str) -> list[str]:
    return re.findall(r"```bash\n(.*?)```", text, flags=re.S)


def test_report_states_offline_scope_and_unit_test_evidence() -> None:
    text = _report_text()
    required_fragments = [
        "未打真 API",
        "命中證據目前 N/A",
        "tests/core/test_prompt_cache.py",
        "timeout 300 .venv/bin/python -m pytest -q tests/core/test_prompt_cache.py",
        "4 passed",
        "ENABLE_PROMPT_CACHING_1H",
        "TI_PROMPT_CACHE_1H",
        "env=None",
        "ANTHROPIC_API_KEY",
        "merge",
    ]
    missing = [fragment for fragment in required_fragments if fragment not in text]
    assert not missing, f"補驗文件缺少離線證據或已知邊界聲明: {missing}"


def test_report_true_api_recheck_uses_metrics_cache_fields() -> None:
    text = _report_text()

    assert re.search(r"curl\s+-sf\s+[\"']?http://localhost:8000/api/metrics\b", text), (
        "驗收要求真 API 補驗走 GET /api/metrics；文件沒有可複製的 /api/metrics curl 指令"
    )
    assert "cache_read_input_tokens" in text, "補驗指令需說明比對 cache_read_input_tokens"
    assert "cache_creation_input_tokens" in text, "補驗指令需說明比對 cache_creation_input_tokens"
    assert "比例" in text or "命中率" in text, "補驗文件需定義 cache_read/cache_creation 的判讀比例"

    rejection_phrases = [
        "`/api/metrics` 不承載 cache token",
        "它**不在** `/api/metrics`",
        "改查 `/api/history`",
    ]
    contradicted = [phrase for phrase in rejection_phrases if phrase in text]
    assert not contradicted, f"文件明確否定驗收指定的 /api/metrics 補驗路徑: {contradicted}"


def test_report_bash_blocks_do_not_use_bare_python_or_pytest() -> None:
    text = _report_text()
    bad: list[tuple[int, str]] = []
    for block_index, block in enumerate(_bash_blocks(text), start=1):
        for line in block.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if re.match(r"^(timeout\s+\d+\s+)?python(\s|$)", stripped):
                bad.append((block_index, line))
            if re.match(r"^(timeout\s+\d+\s+)?pytest(\s|$)", stripped):
                bad.append((block_index, line))

    assert not bad, f"bash 指令區塊仍有裸 python/pytest: {bad}"
