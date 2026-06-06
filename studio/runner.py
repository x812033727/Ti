"""確定性執行工具 —— 與 LLM 解耦，方便單元測試。

集中所有「真的去跑」的邏輯：執行指令（自測 / Demo）、偵測入口、解析執行指令、
以及在 workspace 內建立獨立 git repo 並做階段性 commit。
"""

from __future__ import annotations

import asyncio
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from . import config


@dataclass
class RunOutput:
    command: str
    exit_code: int
    output: str          # stdout + stderr 合併
    timed_out: bool

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


def _truncate(text: str, limit: int | None = None) -> str:
    limit = limit or config.DEMO_MAX_OUTPUT
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…（輸出過長，已截斷，共 {len(text)} 字）"


async def run_command(
    cwd: Path | str, command: str, timeout: int | None = None
) -> RunOutput:
    """在 cwd 執行 shell 指令，合併 stdout/stderr，套用逾時與輸出上限。"""
    timeout = timeout or config.DEMO_TIMEOUT
    proc = await asyncio.create_subprocess_shell(
        command,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return RunOutput(
            command=command,
            exit_code=proc.returncode if proc.returncode is not None else -1,
            output=_truncate(stdout.decode("utf-8", errors="replace")),
            timed_out=False,
        )
    except asyncio.TimeoutError:
        try:
            proc.kill()
            await proc.wait()
        except ProcessLookupError:
            pass
        return RunOutput(
            command=command,
            exit_code=-1,
            output=f"（執行超過 {timeout} 秒逾時，已中止）",
            timed_out=True,
        )


# 常見入口檔（依優先序）
_ENTRY_CANDIDATES = ("main.py", "app.py", "cli.py", "run.py", "__main__.py")


def detect_entrypoint(cwd: Path | str) -> str | None:
    """猜測可執行入口：常見檔名優先，否則 workspace 內唯一的 .py。"""
    root = Path(cwd)
    if not root.exists():
        return None
    for name in _ENTRY_CANDIDATES:
        if (root / name).is_file():
            return name
    pys = [
        p for p in root.rglob("*.py")
        if "test" not in p.name.lower()
        and not any(part in {"__pycache__", ".git"} for part in p.parts)
    ]
    if len(pys) == 1:
        return str(pys[0].relative_to(root))
    return None


def parse_run_command(text: str) -> str | None:
    """從專家文字解析 `執行指令: ...`（也接受 run command / 執行：）。"""
    m = re.search(r"(?:執行指令|執行命令|run command)\s*[:：]\s*(.+)", text, re.I)
    if not m:
        return None
    cmd = m.group(1).strip().strip("`").strip()
    return cmd or None


def resolve_demo_command(cwd: Path | str, declared: str | None) -> str | None:
    """決定 Demo 要跑什麼：優先用宣告的執行指令，否則偵測入口跑 python。"""
    if declared:
        return declared
    entry = detect_entrypoint(cwd)
    return f"python {entry}" if entry else None


# --- git（workspace 內的獨立 repo）-------------------------------------

def _git_available() -> bool:
    return shutil.which("git") is not None


async def git_init(cwd: Path | str) -> bool:
    """在 workspace 內初始化獨立 git repo 並設定 local 身分。回傳是否成功。"""
    if not config.ENABLE_GIT or not _git_available():
        return False
    root = Path(cwd)
    if (root / ".git").exists():
        return True
    r = await run_command(root, "git init -q", timeout=20)
    if not r.ok:
        return False
    await run_command(root, "git config user.email studio@ti.local", timeout=20)
    await run_command(root, "git config user.name 'Ti Studio'", timeout=20)
    # 關閉 commit 簽章，避免外部簽章環境導致 workspace commit 失敗。
    await run_command(root, "git config commit.gpgsign false", timeout=20)
    return True


async def git_commit(cwd: Path | str, message: str) -> str | None:
    """把 workspace 全部變更 commit，回傳短 hash；無變更或失敗回 None。"""
    if not config.ENABLE_GIT or not _git_available():
        return None
    root = Path(cwd)
    if not (root / ".git").exists():
        if not await git_init(root):
            return None
    await run_command(root, "git add -A", timeout=30)
    # 安全處理訊息中的引號
    safe = message.replace('"', "'")
    r = await run_command(root, f'git commit -q -m "{safe}"', timeout=30)
    if not r.ok:
        return None  # 通常是「無變更可提交」
    h = await run_command(root, "git rev-parse --short HEAD", timeout=20)
    return h.output.strip() if h.ok else None
