"""Aggregate latency/token fields from Ti history meta files."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from studio import config

TOKEN_FIELDS = ("prompt", "completion", "total", "cost_usd", "calls", "cache_read", "cache_write")
LATENCY_FIELDS = ("count", "sum_ms", "max_ms")


def _has_nonzero(value: Any) -> bool:
    if isinstance(value, dict):
        return any(_has_nonzero(item) for item in value.values())
    if isinstance(value, list):
        return any(_has_nonzero(item) for item in value)
    return bool(value)


def aggregate_history_meta(history_root: Path) -> dict[str, Any]:
    metas = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(history_root.glob("*.meta.json"))
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
        "history_root": str(history_root),
        "meta_files": len(metas),
        "with_latency": sum(1 for meta in metas if "latency" in meta),
        "with_token_usage": sum(1 for meta in metas if "token_usage" in meta),
        "latency_total": latency_total,
        "token_usage_total": token_total,
        "nonzero_meta_files": nonzero,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--history-root",
        type=Path,
        default=config.HISTORY_ROOT,
        help="history meta 目錄，預設使用 config.HISTORY_ROOT",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    print(json.dumps(aggregate_history_meta(args.history_root), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
