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


def _is_real_llm_response(result: RunResult) -> bool:
    """判斷一次 run 是否真的打到 Anthropic API。

    啟發式（誠實 > 樂觀）：
    - 必須有 `token_usage` payload（無 payload＝SDK 沒回 usage，通常是 fallback/本地）
    - `cost_usd` 必須 > 0（fallback 路徑由專家層塞文字、`total_cost_usd` 為 None／0；
      即便 SDK 撞限流後 fallback，`total_cost_usd` 也不會被填）
    - `prompt_tokens` 必須 > 0（防「duration_ms 有值但 usage 全 0」的退化事件）

    任何一條不符即標記為 `real_api=False`，報告改為誠實標示「未完成／fallback 路徑」，
    不會把 fallback 數字當真 API delta 寫入對比表。
    """
    payload = result.token_usage_payload or {}
    if not payload:
        return False
    if result.cost_usd is None or float(result.cost_usd) <= 0:
        return False
    if int(payload.get("prompt_tokens") or 0) <= 0:
        return False
    return True


def _build_fail_markdown(payload: dict[str, Any]) -> str:
    return "\n".join(
        [
            "# Prompt Cache A/B Report",
            "",
            "- 真實 API：未完成",
            f"- 失敗原因：`{payload.get('error_type', 'Unknown')}` {payload.get('error', '')}",
            "- 補驗所需（任一即可）：",
            "  - `ANTHROPIC_API_KEY` 環境變數已設定（API key 模式），或",
            "  - 已登入的 `claude` CLI（`claude auth login` 訂閱模式；目前腳本環境需走 key）",
            "- 補驗指令：",
            "  ```bash",
            "  # 1) 確認 Anthropic 憑證（API key 模式）",
            '  test -n "$ANTHROPIC_API_KEY" && echo OK || echo MISSING',
            "  # 2) 重跑 A/B 量測（單輪 timeout 60s，整體避免逾時）",
            "  timeout 90 .venv/bin/python scripts/measure_prompt_cache_ab.py \\",
            "      --after-attempts 2 --turn-timeout 30",
            "  ```",
            "",
        ]
    )


def _build_markdown(payload: dict[str, Any]) -> str:
    if payload.get("error"):
        return _build_fail_markdown(payload)

    mode = payload.get("mode", "real")
    synthetic = mode == "dry_run"
    before = payload["before"]
    after = payload["after"]
    delta = None
    if before["ttft_s"] is not None and after["ttft_s"] is not None:
        delta = after["ttft_s"] - before["ttft_s"]
    cache_hit = after["cache_read_input_tokens"] > 0

    if synthetic:
        real_api_line = (
            "- 真實 API：未打（`--dry-run` 模式，純驗腳本流程與報告 schema；真 API 端到端未實測）"
        )
    elif payload.get("real_api"):
        real_api_line = "- 真實 API：是（Claude Agent SDK 正式 `Expert.speak()` 路徑）"
    else:
        # run 完成但成本/usage 顯示走 fallback／離線路徑（撞限流、配額、API 錯誤等）
        real_api_line = (
            "- 真實 API：否（run 完成但 `cost_usd<=0` 或無 `token_usage`，"
            "推測走 fallback／離線路徑；A/B 數字不視為真實 API 對比）"
        )
    if synthetic:
        cache_hit_line = (
            "- after 命中證據：N/A（dry-run 為合成數據，非真實命中，PASS/FAIL 不適用；"
            f"`cache_read_input_tokens={after['cache_read_input_tokens']}` 僅為合成佔位值）"
        )
        ttft_header = "ttft_s（合成佔位）"
        before_ttft_label = "before ttft_s（合成佔位）"
        after_ttft_label = "after ttft_s（合成佔位）"
        delta_label = "after - before（合成佔位）"
    else:
        cache_hit_line = (
            f"- after 命中證據：{'PASS' if cache_hit else 'FAIL'} "
            f"(`cache_read_input_tokens={after['cache_read_input_tokens']}`)"
        )
        ttft_header = "ttft_s"
        before_ttft_label = "before ttft_s"
        after_ttft_label = "after ttft_s"
        delta_label = "after - before"
    lines = [
        "# Prompt Cache A/B Report",
        "",
        real_api_line,
        f"- 模式：`{mode}`",
        f"- model：`{payload['model']}`",
        f"- effort：`{payload['effort']}`",
        f"- role/system_prompt：`{payload['role_key']}` / sha256 `{payload['system_prompt_sha256']}`",
        f"- allowed_tools：`{', '.join(payload['allowed_tools'])}`",
        f"- cwd：`{payload['cwd']}`",
        f"- prompt sha256：`{payload['prompt_sha256']}`",
        cache_hit_line,
        "",
        "| 組別 | DISABLE_PROMPT_CACHING | "
        f"{ttft_header} | cache_read_input_tokens | "
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
            f"- {before_ttft_label}：`{_format_num(before['ttft_s'])}`",
            f"- {after_ttft_label}：`{_format_num(after['ttft_s'])}`",
            f"- {delta_label}：`{_format_num(delta)}` 秒",
            f"- before cache_read_input_tokens：`{before['cache_read_input_tokens']}`",
            f"- after cache_read_input_tokens：`{after['cache_read_input_tokens']}`",
            "",
            "## 補驗方式",
            "",
            "- 這份報告若非真 API，先把憑證準備好再重跑同一腳本。",
            "- `dry_run` 只驗腳本流程與報告 schema；表格與 `ttft_s` 數字為合成佔位值，"
            "不作快取命中結論。",
            "- 建議做法：設定 `ANTHROPIC_API_KEY`，保留同一組 `model` / `effort` / "
            "`system_prompt`，取消 `--dry-run` 後重執行。",
            "- 參考指令：",
            "  ```bash",
            "  timeout 90 .venv/bin/python scripts/measure_prompt_cache_ab.py \\",
            "      --after-attempts 2 --turn-timeout 30",
            "  ```",
            "- 真 API 端到端驗收以 `after` 的 `cache_read_input_tokens > 0` 為命中證據，"
            "再核對 `ttft_s` before/after 差異。",
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


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return parsed


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
        "real_api": False,
        "mode": "real",
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

    if getattr(args, "dry_run", False):
        # Dry-run：不啟動 SDK subprocess、不打 API；以合成資料驗腳本流程與報告 schema。
        # 報告 `real_api=False, mode="dry_run"`，不會被誤標為真實對比。
        return {**meta, **_build_dry_run_payload(prompt, cwd)}

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

    # 誠實標示：所有 run 都通過 `_is_real_llm_response` 啟發式才算真實 API；
    # 任一 run 走 fallback / 離線路徑，real_api 即為 False（不會把 fallback 數字當真 delta）。
    all_real = all(_is_real_llm_response(r) for r in [before, *after_runs])
    return {
        **meta,
        "real_api": all_real,
        "before": asdict(before),
        "after": asdict(after_runs[-1]),
        "after_runs": [asdict(r) for r in after_runs],
    }


def _build_dry_run_payload(prompt: str, cwd: Path) -> dict[str, Any]:
    """合成 dry-run payload：保留真實 schema、不動 `real_api=False`。

    目的：CI/離線環境能在不打 API 的前提下驗證腳本流程（env 操作、報告 markdown、
    變因鎖死）。產出值僅供 schema 對齊，不可用於實際快取效果結論。
    """
    payload_before = {
        "input_tokens": 100,
        "output_tokens": 10,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "duration_api_ms": 1234,
        "total_cost_usd": 0.001,
    }
    payload_after_cold = {
        "input_tokens": 100,
        "output_tokens": 10,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 90,
        "duration_api_ms": 1300,
        "total_cost_usd": 0.001,
    }
    payload_after_warm = {
        "input_tokens": 10,
        "output_tokens": 10,
        "cache_read_input_tokens": 90,
        "cache_creation_input_tokens": 0,
        "duration_api_ms": 200,
        "total_cost_usd": 0.0001,
    }

    def _result(label: str, disable: bool, payload: dict) -> RunResult:
        return RunResult(
            label=label,
            disable_prompt_caching=disable,
            ttft_s=0.123,
            cache_read_input_tokens=int(payload.get("cache_read_input_tokens") or 0),
            cache_creation_input_tokens=int(payload.get("cache_creation_input_tokens") or 0),
            duration_ms=int(payload.get("duration_api_ms") or 0),
            prompt_tokens=int(payload.get("input_tokens") or 0),
            completion_tokens=int(payload.get("output_tokens") or 0),
            total_tokens=int(payload.get("input_tokens") or 0)
            + int(payload.get("output_tokens") or 0),
            cost_usd=payload.get("total_cost_usd"),
            text_chars=len(prompt),
            token_usage_payload={
                "speaker": "dry-run",
                "provider": "claude",
                "model": config.MODEL_FAST,
                "prompt_tokens": int(payload.get("input_tokens") or 0),
                "completion_tokens": int(payload.get("output_tokens") or 0),
                "total_tokens": int(payload.get("input_tokens") or 0)
                + int(payload.get("output_tokens") or 0),
                "cost_usd": payload.get("total_cost_usd"),
                "cache_read": int(payload.get("cache_read_input_tokens") or 0),
                "cache_write": int(payload.get("cache_creation_input_tokens") or 0),
                "duration_ms": int(payload.get("duration_api_ms") or 0),
                "ttft_s": 0.123,
            },
        )

    before = _result("before", True, payload_before)
    after_cold = _result("after", False, payload_after_cold)
    after_warm = _result("after_read_2", False, payload_after_warm)
    return {
        "real_api": False,
        "mode": "dry_run",
        "before": asdict(before),
        "after": asdict(after_warm),
        "after_runs": [asdict(after_cold), asdict(after_warm)],
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role", default="engineer", choices=sorted(BY_KEY))
    parser.add_argument("--model", default=config.MODEL_FAST)
    parser.add_argument("--prompt", default=DEFAULT_PROMPT)
    parser.add_argument("--prompt-file")
    parser.add_argument("--cwd", default=str(DEFAULT_WORKDIR))
    parser.add_argument("--session-id", default="prompt-cache-ab")
    parser.add_argument("--after-attempts", type=_positive_int, default=2)
    parser.add_argument("--turn-timeout", type=float, default=45.0)
    parser.add_argument("--max-turns", type=int, default=2)
    parser.add_argument("--report", default=str(DEFAULT_REPORT))
    parser.add_argument("--raw", default=str(DEFAULT_RAW))
    parser.add_argument(
        "--allow-failure-report",
        action="store_true",
        help="失敗時仍以 exit 0 結束；只供無憑證環境產出移交報告。",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="不打 API：以合成資料驗腳本流程與報告 schema；報告 `real_api=False, mode=dry_run`。",
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
