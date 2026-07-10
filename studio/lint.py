"""寫時 lint：專家每次寫入/編輯 .py 檔後就地 ruff 修復＋回饋殘餘違規（效率強化 A）。

為什麼需要：lint 問題原本要等到 autopilot 收尾閘門（甚至 GitHub CI）才發現——實證
（autopilot._gate_lint docstring）：#249/#496/#364/#367「連續三輪各燒 1-2 小時只為空格」。
專家 session 內沒有寫時 lint，靠專家自律手動跑（同場混用直譯器寫法，不可靠）。本模組在
「寫檔的當下」自動 `ruff check --fix`（safe-only，對齊閘門決策）＋`ruff format`，殘餘
違規以文字回饋讓專家當場修——問題在最便宜的時點被修掉，不再穿越整場 session。

消費端：
- Claude 專家：experts._make_lint_hook（PostToolUse hook，additionalContext 回饋）。
- OpenAI 相容專家：tools.execute 的 write_file/edit_file 成功路徑附加回饋。
- Codex/Antigravity：CLI 自管工具，無接點（明確不做）。

Fail-open 三重保險：非 .py／無 ruff／子程序失敗／逾時一律回 None（靜默）——寫時 lint 是
加值防線不是依賴，絕不能因它擋住寫檔流程（外部非 Python 專案天然無感）。
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import logging
import sys
from pathlib import Path

from . import config

log = logging.getLogger("ti.lint")

# resolve_ruff 的 per-cwd 快取（每次寫檔都會呼叫，find_spec/stat 不必重做）。
_ruff_cache: dict[str, list[str] | None] = {}


def resolve_ruff(cwd: Path) -> list[str] | None:
    """找該 cwd 適用的 ruff 命令：優先專案自帶 venv（吃該專案 pin 的版本），否則
    studio venv 的 ruff（`sys.executable -m ruff`，對齊 autopilot 閘門），皆無回 None。"""
    key = str(cwd)
    if key in _ruff_cache:
        return _ruff_cache[key]
    local = Path(cwd) / ".venv" / "bin" / "ruff"
    if local.is_file():
        cmd: list[str] | None = [str(local)]
    elif importlib.util.find_spec("ruff") is not None:
        cmd = [sys.executable, "-m", "ruff"]
    else:
        cmd = None
    _ruff_cache[key] = cmd
    return cmd


async def _run(cmd: list[str], cwd: Path) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=config.EXPERT_LINT_TIMEOUT)
    except (TimeoutError, asyncio.TimeoutError):
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        raise
    return proc.returncode if proc.returncode is not None else -1, out.decode("utf-8", "replace")


async def lint_file(cwd: Path, file_path: str) -> str | None:
    """對單一 .py 檔跑 ruff 自動修復，回傳給專家的回饋文字；無事可回饋回 None。

    流程：`ruff check --fix`（safe-only，不帶 --unsafe-fixes）→ `ruff format` → 重跑
    `ruff check` 取殘餘違規。cwd 為工作目錄執行，讓 ruff 吃該專案自己的 pyproject 設定。
    回饋三態：殘餘違規 → 違規清單＋「請當場修正」；有自動改寫但全綠 → 提醒重新 Read
    （專家手上的檔案快照已過期，直接 Edit 會 old_string 對不上）；全綠無改寫 → None。
    任何例外（無 ruff/逾時/子程序爆炸）→ None——fail-open，絕不擋工具。
    """
    try:
        if not config.EXPERT_LINT_HOOK:
            return None
        if not str(file_path).endswith((".py", ".pyi")):
            return None
        target = Path(file_path)
        if not target.is_absolute():
            target = Path(cwd) / target
        if not target.is_file():
            return None
        ruff = resolve_ruff(Path(cwd))
        if ruff is None:
            return None
        before = target.read_bytes()
        await _run([*ruff, "check", "--fix", str(target)], Path(cwd))
        await _run([*ruff, "format", str(target)], Path(cwd))
        rc, out = await _run([*ruff, "check", str(target)], Path(cwd))
        changed = target.read_bytes() != before
        if rc != 0 and out.strip():
            head = "[lint] 該檔仍有 ruff 違規（已先自動套用 safe 修復與排版），請當場修正後再繼續："
            tail = "\n（注意：檔案可能已被自動改寫，續編前請重新 Read 該檔。）" if changed else ""
            return f"{head}\n{out.strip()[:2000]}{tail}"
        if changed:
            return "[lint] 檔案已被 ruff 自動修復/排版（內容有變）——續編前請重新 Read 該檔，避免 Edit 對不上舊內容。"
        return None
    except Exception:  # noqa: BLE001 — 寫時 lint 是加值防線不是依賴，任何失敗都靜默放行
        log.debug("寫時 lint 失敗（靜默放行）：%s", file_path, exc_info=True)
        return None
