from __future__ import annotations

import re
from pathlib import Path

from fastapi.testclient import TestClient

from studio import config, history

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


def test_report_true_api_recheck_uses_history_cache_fields() -> None:
    """文件的真 API 補驗路徑必須指向 /api/history/<id>/events，且說明判讀比例。"""
    text = _report_text()

    assert re.search(r"curl\s+-sf\s+[\"']?http://localhost:8000/api/history/", text), (
        "文件沒有可複製的 /api/history/<SESSION_ID>/events curl 指令；"
        "cache token 的補驗路徑必須指向逐場 history endpoint"
    )
    assert "cache_read_input_tokens" in text, "補驗文件需說明 SDK 的 cache_read_input_tokens 映射"
    assert "cache_creation_input_tokens" in text, (
        "補驗文件需說明 SDK 的 cache_creation_input_tokens 映射"
    )
    assert "比例" in text or "命中率" in text, "補驗文件需定義 cache_read/cache_creation 的判讀比例"
    # 文件提到 /api/metrics 時，必須明標其缺口（不聚合 cache 欄位）
    assert "/api/metrics" in text, "文件應提到 /api/metrics 並說明其缺口，而非完全迴避"
    idx = text.index("/api/metrics")
    surrounding = text[max(0, idx - 80) : idx + 300]
    assert any(kw in surrounding for kw in ["不聚合", "缺口", "後續任務"]), (
        "文件提到 /api/metrics 但未說明不聚合 cache 欄位的缺口"
    )


def test_history_events_endpoint_exposes_cache_fields_in_meta(tmp_path, monkeypatch) -> None:
    """`GET /api/history/{id}/events` 實際在 meta 中回傳 cache_read / cache_write，
    確保文件補驗路徑真實可行。"""
    import json as _json

    monkeypatch.setattr(config, "ACCESS_PASSWORD", "")
    monkeypatch.setattr(config, "HISTORY_ROOT", tmp_path / "hist")
    monkeypatch.setattr(config, "WORKSPACE_ROOT", tmp_path / "ws")
    history._reset_meta_cache()
    (tmp_path / "hist").mkdir(parents=True)

    sid = "cache-session"
    fake_meta = {
        "session_id": sid,
        "status": "done",
        "requirement": "test",
        "started_at": 0.0,
        "finished_at": 1.0,
        "n_events": 0,
        "token_usage": {
            "total": {
                "prompt": 1000,
                "completion": 20,
                "total": 1020,
                "calls": 2,
                "cache_read": 300,
                "cache_write": 700,
                "cost_usd": 0.0,
            },
            "by_provider": {},
            "by_model": {},
            "by_role": {},
        },
    }
    (tmp_path / "hist" / f"{sid}.meta.json").write_text(_json.dumps(fake_meta), encoding="utf-8")
    # events 檔不存在也可，iter_events 回傳空 iterator

    from studio.server import app

    data = TestClient(app).get(f"/api/history/{sid}/events").json()
    assert data.get("meta") is not None, "endpoint 應回傳 meta 欄位"
    total = data["meta"]["token_usage"]["total"]
    assert total["cache_read"] == 300, (
        f"文件補驗路徑要求 meta.token_usage.total.cache_read=300，實際={total['cache_read']}"
    )
    assert total["cache_write"] == 700, (
        f"文件補驗路徑要求 meta.token_usage.total.cache_write=700，實際={total['cache_write']}"
    )


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
