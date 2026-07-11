from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from scripts.aggregate_history_meta import aggregate_history_meta
from studio import config, conventions
from studio.roles import BUILTIN_ROLES

ROOT = Path(__file__).resolve().parents[2]
REPORT = ROOT / "docs" / "perf" / "prompt-cache-selection.md"

SONNET_OPUS_CACHE_FLOOR = 1024


def _claude_tool_schema() -> str:
    claude = shutil.which("claude")
    assert claude, "本機找不到 claude CLI，無法重跑文件的 tool schema proxy 估算"
    for parent in Path(claude).resolve().parents:
        schema = parent / "sdk-tools.d.ts"
        if schema.exists():
            return schema.read_text(encoding="utf-8")
    raise AssertionError("找不到 Claude Code 的 sdk-tools.d.ts，無法重跑文件估算")


def _tool_schema_chunk(schema: str, tool: str) -> str:
    interface_by_tool = {
        "Bash": "BashInput",
        "Read": "FileReadInput",
        "Glob": "GlobInput",
        "Grep": "GrepInput",
        "Edit": "FileEditInput",
        "Write": "FileWriteInput",
        "WebFetch": "WebFetchInput",
        "WebSearch": "WebSearchInput",
    }
    iface = interface_by_tool.get(tool)
    if iface is None:
        return ""
    start = schema.index(f"export interface {iface}")
    end = schema.find("\nexport ", start + 1)
    return schema[start : end if end != -1 else len(schema)]


def _rough_tokens(text: str) -> int:
    han = sum("\u4e00" <= char <= "\u9fff" for char in text)
    return round(han + (len(text) - han) / 4)


def test_report_contains_required_selection_claims() -> None:
    assert REPORT.exists(), "缺少 docs/perf/prompt-cache-selection.md"
    text = REPORT.read_text(encoding="utf-8")

    required_fragments = [
        "## 證據快照",
        "200 場",
        "全為 0",
        "/opt/ti-autopilot-work/history",
        "--history-root",
        '"meta_files": 200',
        '"cache_read": 0',
        '"cache_write": 0',
        "無真 API",
        "命中證據 N/A",
        "研究調研",
        "Sonnet / Opus",
        "1024 token",
        "不使用 fake provider",
    ]
    missing = [fragment for fragment in required_fragments if fragment not in text]
    assert not missing, f"選題報告缺少必要聲明: {missing}"


def test_history_all_zero_evidence_is_reproducible_when_data_exists() -> None:
    aggregate = aggregate_history_meta(config.HISTORY_ROOT)

    if aggregate["meta_files"] == 0:
        pytest.skip(
            f"本 workspace 無 history 數據（{aggregate['history_root']}）；"
            "證據以報告快照為準（200 場全零）；有數據的部署環境可重跑以兌現"
        )

    # 有數據時：驗「非零 meta 不存在」與「cache 欄位全零」；不釘場數（場數隨執行增長）
    assert aggregate["nonzero_meta_files"] == [], (
        f"存在非零 latency/token_usage 的 meta：{aggregate['nonzero_meta_files']}"
    )
    assert aggregate["token_usage_total"]["cache_read"] == 0, aggregate
    assert aggregate["token_usage_total"]["cache_write"] == 0, aggregate


def test_shortest_role_prompt_proxy_exceeds_sonnet_opus_cache_floor() -> None:
    schema = _claude_tool_schema()
    estimates: dict[str, int] = {}

    for role in BUILTIN_ROLES:
        rendered = conventions.apply(role, ROOT).system_prompt
        tool_schema = "".join(_tool_schema_chunk(schema, tool) for tool in role.allowed_tools)
        estimates[role.key] = _rough_tokens(rendered + tool_schema)

    shortest_role, shortest_tokens = min(estimates.items(), key=lambda item: item[1])
    assert shortest_tokens >= SONNET_OPUS_CACHE_FLOOR, (
        f"最短角色 {shortest_role} 估算 {shortest_tokens} token，"
        f"未達 Sonnet/Opus prompt cache 最低門檻 {SONNET_OPUS_CACHE_FLOOR}; 全部估算={estimates}"
    )
