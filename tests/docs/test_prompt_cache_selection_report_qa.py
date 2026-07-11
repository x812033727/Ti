from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from studio import config, conventions
from studio.roles import BUILTIN_ROLES

ROOT = Path(__file__).resolve().parents[2]
REPORT = ROOT / "docs" / "perf" / "prompt-cache-selection.md"

SONNET_OPUS_CACHE_FLOOR = 1024
TOKEN_FIELDS = ("prompt", "completion", "total", "cost_usd", "calls", "cache_read", "cache_write")
LATENCY_FIELDS = ("count", "sum_ms", "max_ms")


def _has_nonzero(value) -> bool:
    if isinstance(value, dict):
        return any(_has_nonzero(v) for v in value.values())
    if isinstance(value, list):
        return any(_has_nonzero(v) for v in value)
    return bool(value)


def _history_aggregate(root: Path) -> dict:
    metas = [
        json.loads(path.read_text(encoding="utf-8")) for path in sorted(root.glob("*.meta.json"))
    ]
    token_total = {field: 0 for field in TOKEN_FIELDS}
    latency_total = {field: 0 for field in LATENCY_FIELDS}
    nonzero = []

    for meta in metas:
        usage = (meta.get("token_usage") or {}).get("total") or {}
        latency = (meta.get("latency") or {}).get("total") or {}
        for field in TOKEN_FIELDS:
            token_total[field] += usage.get(field, 0) or 0
        for field in LATENCY_FIELDS:
            latency_total[field] += latency.get(field, 0) or 0
        if _has_nonzero(meta.get("token_usage")) or _has_nonzero(meta.get("latency")):
            nonzero.append(meta.get("session_id") or "<missing-session-id>")

    return {
        "root": str(root),
        "meta_files": len(metas),
        "with_latency": sum(1 for meta in metas if "latency" in meta),
        "with_token_usage": sum(1 for meta in metas if "token_usage" in meta),
        "latency_total": latency_total,
        "token_usage_total": token_total,
        "nonzero_meta_files": nonzero,
    }


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
        "200 場",
        "全為 0",
        "無真 API",
        "命中證據 N/A",
        "研究調研",
        "Sonnet / Opus",
        "1024 token",
        "不使用 fake provider",
    ]
    missing = [fragment for fragment in required_fragments if fragment not in text]
    assert not missing, f"選題報告缺少必要聲明: {missing}"


def test_history_200_all_zero_evidence_is_reproducible_in_workspace() -> None:
    aggregate = _history_aggregate(config.HISTORY_ROOT)

    if not config.HISTORY_ROOT.exists() or aggregate["meta_files"] == 0:
        pytest.skip(
            "history 是 .gitignore 排除的本機執行資料；"
            f"此 workspace 無可重跑 meta，實際聚合={aggregate}"
        )

    assert aggregate["meta_files"] == 200, f"meta 檔數不是 200: {aggregate}"
    assert aggregate["with_latency"] == 200, f"不是每場都有 latency: {aggregate}"
    assert aggregate["with_token_usage"] == 200, f"不是每場都有 token_usage: {aggregate}"
    assert aggregate["latency_total"] == {"count": 0, "sum_ms": 0, "max_ms": 0}, aggregate
    assert aggregate["token_usage_total"] == {field: 0 for field in TOKEN_FIELDS}, aggregate
    assert aggregate["nonzero_meta_files"] == [], aggregate


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
