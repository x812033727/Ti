"""確定性執行工具 —— 與 LLM 解耦，方便單元測試。

集中所有「真的去跑」的邏輯：執行指令（自測 / Demo）、偵測入口、解析執行指令、
以及在 workspace 內建立獨立 git repo 並做階段性 commit。
"""

from __future__ import annotations

import asyncio
import os
import re
import shlex
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from . import config

# 解析「以 python 開頭」的指令用：把直譯器 token 換成實際可用的執行檔。
_PY_PREFIX = re.compile(r"\s*(python3?|py)\b")


def _executable_command(command: str) -> str:
    """若指令以 python/python3 開頭但該名稱不在 PATH，改用 sys.executable 執行。

    確保在只有 `python3`（無 `python`）的環境也能跑自測 / Demo；顯示用的原始指令不變。
    """
    m = _PY_PREFIX.match(command)
    if not m:
        return command
    tok = m.group(1)
    if shutil.which(tok):
        return command
    return command[: m.start(1)] + shlex.quote(sys.executable) + command[m.end(1) :]


@dataclass
class RunOutput:
    command: str
    exit_code: int
    output: str  # stdout + stderr 合併
    timed_out: bool

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


def _truncate(text: str, limit: int | None = None) -> str:
    limit = limit or config.DEMO_MAX_OUTPUT
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…（輸出過長，已截斷，共 {len(text)} 字）"


def _bwrap_prefix(cwd: Path | str) -> list[str]:
    """bubblewrap argv 前綴：整個 host 唯讀、只有 workspace 可寫、獨立 PID namespace。

    新 PID namespace 讓沙箱內的指令看不到也殺不到主機進程（含正式服務）；
    `--ro-bind / /` 讓 python/node/git 等執行檔可用但主機檔系唯讀。預設 `--unshare-net`
    斷網（Demo 不需網路），設 TI_SANDBOX_NET=1 可放行。
    """
    cwd = str(cwd)
    cache = os.path.join(os.path.expanduser("~"), ".cache")
    args = [
        config.SANDBOX_BWRAP,
        "--ro-bind",
        "/",
        "/",
        "--dev",
        "/dev",
        "--proc",
        "/proc",
        "--tmpfs",
        "/tmp",
        "--tmpfs",
        cache,
        "--bind",
        cwd,
        cwd,
        "--chdir",
        cwd,
        "--unshare-pid",
        "--die-with-parent",
        "--new-session",
    ]
    if not config.SANDBOX_NET:
        args.append("--unshare-net")
    return args


async def run_command(
    cwd: Path | str, command: str, timeout: int | None = None, sandbox: bool | None = None
) -> RunOutput:
    """在 cwd 執行 shell 指令，合併 stdout/stderr，套用逾時與輸出上限。

    sandbox=None 時取 config.SANDBOX_ENABLED；啟用時用 bubblewrap 把指令關進
    workspace 沙箱（新 PID namespace、host 唯讀）。沙箱啟用但 bwrap 不存在則
    fail-closed：拒絕執行，絕不以 root 裸跑。
    """
    timeout = timeout or config.DEMO_TIMEOUT
    use_sandbox = config.SANDBOX_ENABLED if sandbox is None else sandbox
    inner = _executable_command(command)
    if use_sandbox:
        if not config._sandbox_available():
            return RunOutput(
                command=command,
                exit_code=-1,
                output=f"（沙箱已啟用但找不到 {config.SANDBOX_BWRAP}，為安全拒絕執行）",
                timed_out=True,
            )
        proc = await asyncio.create_subprocess_exec(
            *_bwrap_prefix(cwd),
            "/bin/sh",
            "-c",
            inner,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    else:
        proc = await asyncio.create_subprocess_shell(
            inner,
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
        p
        for p in root.rglob("*.py")
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
    r = await run_command(root, "git init -q", timeout=20, sandbox=False)
    if not r.ok:
        return False
    await run_command(root, "git config user.email studio@ti.local", timeout=20, sandbox=False)
    await run_command(root, "git config user.name 'Ti Studio'", timeout=20, sandbox=False)
    # 關閉 commit 簽章，避免外部簽章環境導致 workspace commit 失敗。
    await run_command(root, "git config commit.gpgsign false", timeout=20, sandbox=False)
    return True


# --- clone 既有 GitHub 倉庫到 workspace --------------------------------

# 僅接受 github.com 的 https 倉庫網址（避免任意主機 / 路徑，降低風險）。
_REPO_RE = re.compile(r"^https://github\.com/[\w.-]+/[\w.-]+?(?:\.git)?/?$", re.I)
_BRANCH_RE = re.compile(r"^[\w./-]{1,200}$")


def is_valid_repo_url(url: str) -> bool:
    return bool(_REPO_RE.match((url or "").strip()))


def build_clone_url(url: str, token: str | None) -> str:
    """私有倉庫時把 token 注入 https URL；否則原樣回傳。"""
    url = (url or "").strip()
    if token and url.startswith("https://github.com/"):
        return url.replace("https://", f"https://x-access-token:{token}@", 1)
    return url


async def git_clone(
    url: str, dest: Path | str, token: str | None = None, branch: str | None = None
) -> RunOutput:
    """把 GitHub 倉庫 clone 到（空的）dest 目錄。回傳 RunOutput（output 已遮蔽 token）。"""
    if not _git_available():
        return RunOutput("git clone", -1, "（環境沒有 git，無法 clone）", False)
    authed = build_clone_url(url, token)
    parts = ["git", "clone", "--depth", "1"]
    if branch and _BRANCH_RE.match(branch):
        parts += ["--branch", branch]
    cmd = " ".join(shlex.quote(p) for p in parts) + f" {shlex.quote(authed)} ."
    result = await run_command(dest, cmd, timeout=180, sandbox=False)
    # 避免 token 出現在回報的指令 / 輸出裡
    if token:
        result.output = result.output.replace(token, "***")
    result.command = "git clone " + (url or "").strip()
    return result


async def git_commit(cwd: Path | str, message: str) -> str | None:
    """把 workspace 全部變更 commit，回傳短 hash；無變更或失敗回 None。"""
    if not config.ENABLE_GIT or not _git_available():
        return None
    root = Path(cwd)
    if not (root / ".git").exists():
        if not await git_init(root):
            return None
    await run_command(root, "git add -A", timeout=30, sandbox=False)
    # 安全處理訊息中的引號
    safe = message.replace('"', "'")
    r = await run_command(root, f'git commit -q -m "{safe}"', timeout=30, sandbox=False)
    if not r.ok:
        return None  # 通常是「無變更可提交」
    h = await run_command(root, "git rev-parse --short HEAD", timeout=20, sandbox=False)
    return h.output.strip() if h.ok else None
