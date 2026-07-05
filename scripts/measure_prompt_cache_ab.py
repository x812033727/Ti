#!/usr/bin/env python3
"""Claude Agent SDK prompt caching A/B measurement.

Runs the production ``Expert.speak()`` path twice or more:

- before: ``DISABLE_PROMPT_CACHING=1``
- after: default prompt caching env

The measured fields come from the emitted ``token_usage`` event, so this script
validates the same ``ttft_s`` and cache token plumbing used by the app.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from studio import config  # noqa: E402
from studio.experts import Expert  # noqa: E402
from studio.roles import BY_KEY, effective_tools  # noqa: E402

DEFAULT_REPORT = ROOT / "docs" / "PROMPT_CACHE_AB_REPORT.md"
DEFAULT_RAW = ROOT / ".qa_artifacts" / "prompt_cache_ab" / "latest.json"
DEFAULT_WORKDIR = ROOT / ".qa_artifacts" / "prompt_cache_ab" / "workdir"
DEFAULT_PROMPT = "請不要使用任何工具，不要讀寫檔案。只用繁體中文回覆一行：prompt-cache-ab-ok。"


@dataclass
class RunResult:
    label: str
    disable_prompt_caching: bool
    ttft_s: float | None
    cache_read_input_tokens: int
    cache_creation_input_tokens: int
    duration_ms: int | None
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cost_usd: float | None
    text_chars: int
    token_usage_payload: dict[str, Any]


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _load_prompt(path: str | None, inline: str | None) -> str:
    if path:
        return Path(path).read_text(encoding="utf-8")
    return inline or DEFAULT_PROMPT


def _last_token_usage_payload(emitted: list[dict[str, Any]]) -> dict[str, Any]:
    for ev in reversed(emitted):
        if ev.get("type") == "token_usage":
            payload = ev.get("payload") or {}
            if isinstance(payload, dict):
                return payload
    raise RuntimeError("本次呼叫未收到 token_usage 事件，無法讀取 ttft_s/cache_read")


def _set_cache_env(disabled: bool) -> str | None:
    previous = os.environ.get("DISABLE_PROMPT_CACHING")
    if disabled:
        os.environ["DISABLE_PROMPT_CACHING"] = "1"
    else:
        os.environ.pop("DISABLE_PROMPT_CACHING", None)
    return previous


def _restore_cache_env(previous: str | None) -> None:
    if previous is None:
        os.environ.pop("DISABLE_PROMPT_CACHING", None)
    else:
        os.environ["DISABLE_PROMPT_CACHING"] = previous


async def _run_one(
    *,
    label: str,
    disable_prompt_caching: bool,
    role_key: str,
    model: str,
    prompt: str,
    cwd: Path,
    session_id: str,
) -> RunResult:
    role = BY_KEY[role_key]
    emitted: list[dict[str, Any]] = []

    async def broadcast(ev) -> None:  # noqa: ANN001 - StudioEvent duck type
        emitted.append(ev.to_dict())

    previous = _set_cache_env(disable_prompt_caching)
    expert = Expert(role, session_id, cwd, model=model)
    try:
        text = await expert.speak(prompt, broadcast)
    finally:
        try:
            await expert.stop()
        finally:
            _restore_cache_env(previous)

    payload = _last_token_usage_payload(emitted)
    return RunResult(
        label=label,
        disable_prompt_caching=disable_prompt_caching,
        ttft_s=payload.get("ttft_s"),
        cache_read_input_tokens=int(payload.get("cache_read") or 0),
        cache_creation_input_tokens=int(payload.get("cache_write") or 0),
        duration_ms=payload.get("duration_ms"),
        prompt_tokens=int(payload.get("prompt_tokens") or 0),
        completion_tokens=int(payload.get("completion_tokens") or 0),
        total_tokens=int(payload.get("total_tokens") or 0),
        cost_usd=payload.get("cost_usd"),
        text_chars=len(text),
        token_usage_payload=payload,
    )


def _format_num(value: float | int | None, digits: int = 3) -> str:
    if value is None:
        return "None"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def _build_markdown(payload: dict[str, Any]) -> str:
    if payload.get("error"):
        return "\n".join(
            [
                "# Prompt Cache A/B Report",
                "",
                "- 真實 API：未完成",
                f"- 失敗原因：`{payload['error_type']}` {payload['error']}",
                "- 補驗方式：設定 Anthropic API key 或 Claude CLI 登入憑證後重跑本腳本。",
                "",
                "```bash",
                "timeout 60 .venv/bin/python scripts/measure_prompt_cache_ab.py",
                "```",
                "",
            ]
        )

    before = payload["before"]
    after = payload["after"]
    delta = None
    if before["ttft_s"] is not None and after["ttft_s"] is not None:
        delta = after["ttft_s"] - before["ttft_s"]
    cache_hit = after["cache_read_input_tokens"] > 0
    lines = [
        "# Prompt Cache A/B Report",
        "",
        "- 真實 API：是（Claude Agent SDK 正式 `Expert.speak()` 路徑）",
        f"- model：`{payload['model']}`",
        f"- effort：`{payload['effort']}`",
        f"- role/system_prompt：`{payload['role_key']}` / sha256 `{payload['system_prompt_sha256']}`",
        f"- allowed_tools：`{', '.join(payload['allowed_tools'])}`",
        f"- cwd：`{payload['cwd']}`",
        f"- prompt sha256：`{payload['prompt_sha256']}`",
        f"- after 命中證據：{'PASS' if cache_hit else 'FAIL'} "
        f"(`cache_read_input_tokens={after['cache_read_input_tokens']}`)",
        "",
        "| 組別 | DISABLE_PROMPT_CACHING | ttft_s | cache_read_input_tokens | "
        "cache_creation_input_tokens | duration_ms | prompt_tokens | completion_tokens |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in [before, *payload.get("after_runs", [])]:
        lines.append(
            f"| {row['label']} | "
            f"{'1' if row['disable_prompt_caching'] else 'unset'} | "
            f"{_format_num(row['ttft_s'])} | "
            f"{row['cache_read_input_tokens']} | "
            f"{row['cache_creation_input_tokens']} | "
            f"{_format_num(row['duration_ms'], 0)} | "
            f"{row['prompt_tokens']} | "
            f"{row['completion_tokens']} |"
        )
    lines.extend(
        [
            "",
            "## Before/After 對比",
            "",
            f"- before ttft_s：`{_format_num(before['ttft_s'])}`",
            f"- after ttft_s：`{_format_num(after['ttft_s'])}`",
            f"- after - before：`{_format_num(delta)}` 秒",
            f"- before cache_read_input_tokens：`{before['cache_read_input_tokens']}`",
            f"- after cache_read_input_tokens：`{after['cache_read_input_tokens']}`",
            "",
            "註：`ttft_s` 是本專案串流包裝層量到的首個內容事件時間，適合看同路徑 A/B delta；"
            "絕對值不宣稱等同 provider 原生 TTFT。",
            "",
        ]
    )
    return "\n".join(lines)


def _write_outputs(report_path: Path, raw_path: Path, payload: dict[str, Any]) -> None:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(_build_markdown(payload), encoding="utf-8")
    raw_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    role = BY_KEY[args.role]
    prompt = _load_prompt(args.prompt_file, args.prompt)
    cwd = Path(args.cwd).resolve()
    cwd.mkdir(parents=True, exist_ok=True)

    # Keep the measurement bounded and stable regardless of deployment defaults.
    config.TURN_IDLE_TIMEOUT = float(args.turn_timeout)
    config.TURN_HARD_TIMEOUT = float(args.turn_timeout)
    config.MAX_TURNS_PER_TURN = int(args.max_turns)

    meta = {
        "real_api": True,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "role_key": args.role,
        "model": args.model,
        "effort": "agent_sdk_default",
        "system_prompt_sha256": _sha256_text(role.system_prompt),
        "allowed_tools": effective_tools(role),
        "prompt_sha256": _sha256_text(prompt),
        "cwd": str(cwd),
        "cli": shutil.which("claude") or "",
        "after_attempts_requested": args.after_attempts,
    }

    before = await _run_one(
        label="before",
        disable_prompt_caching=True,
        role_key=args.role,
        model=args.model,
        prompt=prompt,
        cwd=cwd,
        session_id=args.session_id,
    )

    after_runs: list[RunResult] = []
    for idx in range(args.after_attempts):
        label = "after" if idx == 0 else f"after_read_{idx + 1}"
        result = await _run_one(
            label=label,
            disable_prompt_caching=False,
            role_key=args.role,
            model=args.model,
            prompt=prompt,
            cwd=cwd,
            session_id=args.session_id,
        )
        after_runs.append(result)
        if result.cache_read_input_tokens > 0:
            break

    return {
        **meta,
        "before": asdict(before),
        "after": asdict(after_runs[-1]),
        "after_runs": [asdict(r) for r in after_runs],
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role", default="engineer", choices=sorted(BY_KEY))
    parser.add_argument("--model", default=config.MODEL_FAST)
    parser.add_argument("--prompt", default=DEFAULT_PROMPT)
    parser.add_argument("--prompt-file")
    parser.add_argument("--cwd", default=str(DEFAULT_WORKDIR))
    parser.add_argument("--session-id", default="prompt-cache-ab")
    parser.add_argument("--after-attempts", type=int, default=2)
    parser.add_argument("--turn-timeout", type=float, default=45.0)
    parser.add_argument("--max-turns", type=int, default=2)
    parser.add_argument("--report", default=str(DEFAULT_REPORT))
    parser.add_argument("--raw", default=str(DEFAULT_RAW))
    parser.add_argument(
        "--allow-failure-report",
        action="store_true",
        help="失敗時仍以 exit 0 結束；只供無憑證環境產出移交報告。",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    report_path = Path(args.report)
    raw_path = Path(args.raw)
    try:
        payload = asyncio.run(_run(args))
        _write_outputs(report_path, raw_path, payload)
        before = payload["before"]
        after = payload["after"]
        print(f"report={report_path}")
        print(f"raw={raw_path}")
        print(
            "before "
            f"ttft_s={_format_num(before['ttft_s'])} "
            f"cache_read_input_tokens={before['cache_read_input_tokens']}"
        )
        print(
            "after "
            f"ttft_s={_format_num(after['ttft_s'])} "
            f"cache_read_input_tokens={after['cache_read_input_tokens']}"
        )
        return 0
    except Exception as exc:  # noqa: BLE001 - script boundary must leave a clear report
        payload = {
            "real_api": False,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "error_type": type(exc).__name__,
            "error": str(exc),
        }
        _write_outputs(report_path, raw_path, payload)
        print(f"report={report_path}", file=sys.stderr)
        print(f"raw={raw_path}", file=sys.stderr)
        print(f"error={type(exc).__name__}: {exc}", file=sys.stderr)
        return 0 if args.allow_failure_report else 2


if __name__ == "__main__":
    raise SystemExit(main())
