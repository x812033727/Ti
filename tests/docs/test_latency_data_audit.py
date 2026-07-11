"""QA 守門：延遲數據實查報告必須和 history 實際狀態一致。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

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

    history/ 已 gitignore（執行期資料），lane worktree 及 CI 環境通常不存在。
    缺失時跳過本測試；在含真實 session 資料的主工作目錄執行可完整驗證。
    """
    meta_paths = _history_meta_paths()
    jsonl_paths = _history_jsonl_paths()

    if len(meta_paths) < 200:
        pytest.skip(
            f"history/ 僅含 {len(meta_paths)} 筆 meta（需 200）；"
            "history/ 已 gitignore（執行期資料），lane worktree 及 CI 環境不含此目錄，"
            "請在含 200 場 history 的主工作目錄執行以完整驗證。"
        )

    text = DOC.read_text(encoding="utf-8")
    metas = [_load_json(path) for path in meta_paths]

    assert len(metas) == 200
    assert len(jsonl_paths) == 200

    latency_total_counts = [
        meta.get("latency", {}).get("total", {}).get("count", 0) for meta in metas
    ]
    assert latency_total_counts == [0] * 200
    assert all(not meta.get("latency", {}).get("by_role") for meta in metas)
    assert all(not meta.get("latency", {}).get("by_model") for meta in metas)
    assert all(not meta.get("latency", {}).get("by_provider") for meta in metas)
    assert all(meta.get("token_usage", {}).get("total", {}).get("calls", 0) == 0 for meta in metas)
    assert _token_usage_event_count(jsonl_paths) == 0

    assert "meta 總數: 200" in text
    assert "latency.total.count == 0: 200" in text
    assert "latency.total.count  > 0: 0" in text
    assert "jsonl 總數: 200" in text
    assert "token_usage 事件數: 0" in text
    assert "**200 / 200**" in text


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
