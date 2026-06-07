"""確定性執行工具 —— 與 LLM 解耦，方便單元測試。

集中所有「真的去跑」的邏輯：執行指令（自測 / Demo）、偵測入口、解析執行指令、
以及在 workspace 內建立獨立 git repo 並做階段性 commit。
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shlex
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from . import config

log = logging.getLogger("ti.runner")

# workspace 內 git 身分（git_init 寫入 .git/config；commit 另帶 -c 兜底，
# 涵蓋 clone 流程下 .git 已存在、git_init no-op 而 local identity 缺失的情形）。
_GIT_USER_NAME = "Ti Studio"
_GIT_USER_EMAIL = "studio@ti.local"

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


def _sandbox_blocked(label: str) -> RunOutput:
    """沙箱啟用但 bwrap 不存在時的 fail-closed 結果（shell/exec 兩路共用）。"""
    return RunOutput(
        command=label,
        exit_code=-1,
        output=f"（沙箱已啟用但找不到 {config.SANDBOX_BWRAP}，為安全拒絕執行）",
        timed_out=True,
    )


async def _finalize_proc(
    proc: asyncio.subprocess.Process, label: str, timeout: int
) -> RunOutput:
    """共用收尾：communicate + 逾時 kill + 輸出截斷。

    `label` 為 RunOutput.command 顯示用字串，由呼叫端決定（shell 傳原始指令、
    exec 傳簡短標籤），不在此寫死，避免兩路邏輯分叉。
    """
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return RunOutput(
            command=label,
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
            command=label,
            exit_code=-1,
            output=f"（執行超過 {timeout} 秒逾時，已中止）",
            timed_out=True,
        )


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
            return _sandbox_blocked(command)
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
    return await _finalize_proc(proc, command, timeout)


async def run_command_exec(
    cwd: Path | str,
    argv: list[str],
    timeout: int | None = None,
    sandbox: bool | None = None,
    label: str | None = None,
) -> RunOutput:
    """以參數式（argv list）執行指令，不經 /bin/sh，shell metacharacters 天然安全。

    與 run_command 並存、共用收尾邏輯；訊息／參數以 argv 單一元素傳遞，免跳脫、
    多行與特殊字元原樣保留。沙箱時前置 _bwrap_prefix 後直接接 argv（不再包
    /bin/sh -c）；非沙箱時以 cwd=str(cwd) 執行。沙箱啟用但 bwrap 不存在則
    fail-closed，語意與 run_command 一致。

    label 為 RunOutput.command 的顯示標籤（如 "git commit"），預設取 argv[0]，
    不內插完整參數，避免多行訊息污染日誌。
    """
    if not argv:
        raise ValueError("run_command_exec 需要非空的 argv")
    timeout = timeout or config.DEMO_TIMEOUT
    use_sandbox = config.SANDBOX_ENABLED if sandbox is None else sandbox
    display = label or argv[0]
    if use_sandbox:
        if not config._sandbox_available():
            return _sandbox_blocked(display)
        proc = await asyncio.create_subprocess_exec(
            *_bwrap_prefix(cwd),
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    else:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    return await _finalize_proc(proc, display, timeout)


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
# 首字元不允許 '-'：避免 `--foo` / `-o` 這類「以選項開頭」的 branch 通過過濾，
# 進而在 `git clone ... --branch <branch>` 被 git 當成參數注入（如 --upload-pack）。
_BRANCH_RE = re.compile(r"^[\w./][\w./-]{0,199}$")


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
    """把 workspace 全部變更 commit，回傳短 hash；無變更或失敗回 None。

    三步全走參數式 exec（`run_command_exec`），commit 訊息以 argv 單一元素傳遞，
    shell 不參與解析——backtick / `$(...)` / `;` / 換行 等一律當純文字，免跳脫、
    多行原樣保留。沙箱沿用 `SANDBOX_ENABLED`；bwrap 缺失則 fail-closed 回 None。
    """
    if not config.ENABLE_GIT or not _git_available():
        return None
    root = Path(cwd)
    if not (root / ".git").exists():
        if not await git_init(root):
            return None

    add = await run_command_exec(root, ["git", "add", "-A"], timeout=30, label="git add")
    if not add.ok:
        # 沙箱啟用但 bwrap 缺失會在此 fail-closed；記一筆便於排查。
        log.warning("git add 失敗（exit=%s, timed_out=%s）：%s",
                    add.exit_code, add.timed_out, add.output)
        return None

    # commit 直接帶 git identity 兜底，消滅 clone 流程下 identity 缺失整類失敗。
    r = await run_command_exec(
        root,
        [
            "git",
            "-c", f"user.name={_GIT_USER_NAME}",
            "-c", f"user.email={_GIT_USER_EMAIL}",
            "-c", "commit.gpgsign=false",
            "commit", "-q", "-m", message,
        ],
        timeout=30,
        label="git commit",
    )
    if not r.ok:
        return None  # 通常是「無變更可提交」

    h = await run_command_exec(
        root, ["git", "rev-parse", "--short", "HEAD"], timeout=20, label="git rev-parse"
    )
    return h.output.strip() if h.ok else None
