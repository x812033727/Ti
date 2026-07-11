"""QA 守門：延遲數據實查報告必須和 history 實際狀態一致。"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOC = ROOT / "docs" / "latency-data-audit.md"
HISTORY = ROOT / "history"


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _history_meta_paths() -> list[Path]:
    return sorted(HISTORY.glob("*.meta.json"))


def _history_jsonl_paths() -> list[Path]:
    return sorted(HISTORY.glob("*.jsonl"))


def _token_usage_event_count(paths: list[Path]) -> int:
    total = 0
    for path in paths:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip() and json.loads(line).get("type") == "token_usage":
                total += 1
    return total


def _declared_int(text: str, pattern: str) -> int:
    match = re.search(pattern, text)
    assert match, f"report is missing declared number for pattern: {pattern}"
    return int(match.group(1))


def test_latency_data_audit_doc_exists_and_has_reproducible_commands():
    text = DOC.read_text(encoding="utf-8")

    assert "可重現查核指令" in text
    assert text.count("timeout 60") >= 4
    assert ".venv/bin/python -c" in text
    assert "history/*.meta.json" in text
    assert "history/*.jsonl" in text
    assert "python3" not in text
    # 環境前提聲明：報告須說明 history/ 為 gitignored 執行期資料
    assert "gitignore" in text or "執行期" in text


def test_latency_data_audit_numbers_match_history_zero_state():
    """驗證報告宣稱數字與 history/ 實際資料一致。

    任務要求是「實查本工作目錄」，不能在資料缺失或筆數不符時跳過；
    否則報告可宣稱 200 筆但 lane 只含 0/6 筆仍被放行。
    """
    meta_paths = _history_meta_paths()
    jsonl_paths = _history_jsonl_paths()
    text = DOC.read_text(encoding="utf-8")
    metas = [_load_json(path) for path in meta_paths]

    assert "檔數以查核指令當下輸出為準" in text
    assert "不以固定檔數作成效結論" in text
    assert "6 場" not in text
    assert "12 場" not in text
    assert "200 場" not in text

    latency_total_counts = [
        meta.get("latency", {}).get("total", {}).get("count", 0) for meta in metas
    ]
    assert "latency.total.count == 0" in text
    assert "latency.total.count  > 0: 0" in text
    assert len(metas) > 0
    assert latency_total_counts == [0] * len(metas)
    assert all(not meta.get("latency", {}).get("by_role") for meta in metas)
    assert all(not meta.get("latency", {}).get("by_model") for meta in metas)
    assert all(not meta.get("latency", {}).get("by_provider") for meta in metas)
    assert all(meta.get("token_usage", {}).get("total", {}).get("calls", 0) == 0 for meta in metas)
    assert _declared_int(text, r"token_usage 事件數:\s*(\d+)") == _token_usage_event_count(
        jsonl_paths
    )


def test_latency_data_audit_declares_research_basis_after_invalid_data():
    text = DOC.read_text(encoding="utf-8")

    assert "前置數據不成立" in text
    assert "選題依據改為研究證據" in text
    assert "輸出 token" in text
    assert "延遲驅動" in text
    assert "~10ms/token" in text
    assert "從量測數據找最慢環節" in text
    assert "無法執行" in text or "不成立" in text
    # 高工建議：假設推算須有免責宣告
    assert "假設" in text or "推算" in text
