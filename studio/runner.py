"""確定性執行工具 —— 與 LLM 解耦，方便單元測試。

集中所有「真的去跑」的邏輯：執行指令（自測 / Demo）、偵測入口、解析執行指令、
以及在 workspace 內建立獨立 git repo 並做階段性 commit。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import shlex
import shutil
import signal
import sys
from dataclasses import dataclass
from pathlib import Path

try:
    import resource  # POSIX 專有；非 POSIX（如 Windows）下資源上限整段 no-op。
except ImportError:  # pragma: no cover - 僅非 POSIX 平台
    resource = None  # type: ignore[assignment]

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


def _worktree_common_git_dir(cwd: Path | str) -> Path | None:
    """cwd 是 git linked worktree 時，回傳其共用 git 目錄（主 repo 的 .git）；否則 None。

    並行 lane 在 workspace 外的兄弟目錄開 worktree，其 `.git` 是檔案而非目錄，內容形如
    `gitdir: <主 repo>/.git/worktrees/<name>`。在沙箱裡 `git add/commit/merge` 要寫的
    index.lock、refs、objects 都落在那個共用 .git 底下——它不在 lane cwd 之內，預設只 bind
    cwd 可寫時會踩到 `--ro-bind / /` 的唯讀面而失敗（「Read-only file system」），導致 lane
    分支拿不到 commit、波次合併變成 no-op。故偵測出共用 .git 路徑，交由 _bwrap_prefix 一併綁可寫。
    一般 repo（`.git` 為目錄、就在 cwd 內）回 None，行為不變。
    """
    gitfile = Path(cwd) / ".git"
    try:
        if not gitfile.is_file():
            return None  # 一般 repo：.git 是目錄，已隨 cwd 綁為可寫
        text = gitfile.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    if not text.startswith("gitdir:"):
        return None
    admin = Path(text[len("gitdir:") :].strip())
    if not admin.is_absolute():
        admin = (Path(cwd) / admin).resolve()
    # admin＝<共用 .git>/worktrees/<name>；commondir 檔給出回到共用 .git 的相對路徑（通常 ../..）。
    commondir = admin / "commondir"
    try:
        if commondir.is_file():
            rel = commondir.read_text(encoding="utf-8", errors="replace").strip()
            return (admin / rel).resolve()
    except OSError:
        pass
    return admin.parent.parent  # 後備：worktrees/<name> 往上兩層即共用 .git


def _bwrap_prefix(cwd: Path | str, net: bool | None = None) -> list[str]:
    """bubblewrap argv 前綴：整個 host 唯讀、只有 workspace 可寫、獨立 PID namespace。

    新 PID namespace 讓沙箱內的指令看不到也殺不到主機進程（含正式服務）；
    `--ro-bind / /` 讓 python/node/git 等執行檔可用但主機檔系唯讀。預設 `--unshare-net`
    斷網（Demo 不需網路），設 TI_SANDBOX_NET=1 可放行；net 參數可逐次覆寫（HTTP Demo 須與
    host 共享 loopback 才探測得到，傳 net=True）。cwd 為並行 lane 的 linked worktree 時，
    額外把共用 git 目錄（主 repo 的 .git）綁為可寫，否則 worktree 內的 git 寫入會踩到唯讀面。
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
    ]
    common_git = _worktree_common_git_dir(cwd)
    if common_git is not None and common_git.exists():
        args += ["--bind", str(common_git), str(common_git)]
    args += [
        "--chdir",
        cwd,
        "--unshare-pid",
        "--die-with-parent",
        "--new-session",
    ]
    allow_net = config.SANDBOX_NET if net is None else net
    if not allow_net:
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


def kill_process_group(proc: asyncio.subprocess.Process) -> None:
    """逾時時 SIGKILL 整個 process group。

    只 `proc.kill()` 殺得到直屬子程序（非沙箱下是 `/bin/sh`），它再 spawn 的工作程序
    （python/node…）是孫程序，會變孤兒繼續吃資源——長跑的自托管/autopilot 上每個逾時
    的 Demo 就洩漏一個。子程序皆以 start_new_session=True 啟動、自成 group leader，故可
    對整組送訊號。取不到 pgid（已歿）或平台不支援（如 Windows 無 killpg）時，退回只殺
    直屬子程序。沙箱路徑殺 bwrap 即可，其 `--die-with-parent`＋PID namespace 會連帶清掉。
    """
    pid = proc.pid
    if pid is not None and hasattr(os, "killpg") and hasattr(os, "getpgid"):
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
            return
        except (ProcessLookupError, PermissionError, OSError):
            pass
    try:
        proc.kill()
    except ProcessLookupError:
        pass


def _rlimit_preexec():
    """產生「fork 後、exec 前套用資源上限」的 preexec_fn；停用／無 resource／全 0 時回 None。

    補 bwrap 沒有的記憶體／CPU／檔案大小防線——bwrap 缺席（如本機無 /usr/bin/bwrap）時，
    這是唯一能擋住失控指令吃爆主機的防線；bwrap 啟用時 RLIMIT 也會經 exec 繼承進沙箱子進程。
    closure body 只做 setrlimit syscalls（fork 安全：不配置記憶體、不拿鎖、不 import），各上限
    0＝略過。移植自 ti-studio evaluator._preexec，刻意「不」呼叫 os.setsid()——兩個 subprocess
    分支已 start_new_session=True 自成 group leader，kill_process_group 靠它整組收屍，重複
    setsid 反而干擾。
    """
    if resource is None or not config.RLIMITS_ENABLED:
        return None
    mem_mb = config.RLIMIT_MEM_MB
    cpu_s = config.RLIMIT_CPU_S
    fsize_mb = config.RLIMIT_FSIZE_MB
    if not (mem_mb or cpu_s or fsize_mb):
        return None

    def _inner() -> None:
        def _try(res, soft_hard):
            try:
                resource.setrlimit(res, soft_hard)
            except (ValueError, OSError):
                pass

        if mem_mb:
            b = mem_mb * 1024 * 1024
            _try(resource.RLIMIT_AS, (b, b))  # 位址空間（記憶體）
        if cpu_s:
            _try(resource.RLIMIT_CPU, (cpu_s, cpu_s))  # CPU 時間（秒）
        if fsize_mb:
            b = fsize_mb * 1024 * 1024
            _try(resource.RLIMIT_FSIZE, (b, b))  # 可寫單檔大小上限
        _try(resource.RLIMIT_CORE, (0, 0))  # 關 core dump
        # 限制進程數盡力擋 fork bomb；不放寬現有上限（避免共享 UID 環境誤殺）。註：root 下
        # RLIMIT_NPROC 以 real UID 全域計、實為盡力而為——根本防護仍靠 OS 層／PID namespace。
        try:
            soft, hard = resource.getrlimit(resource.RLIMIT_NPROC)
            cap = 256 if soft == resource.RLIM_INFINITY else min(soft, 256)
            resource.setrlimit(resource.RLIMIT_NPROC, (cap, hard))
        except (ValueError, OSError):
            pass

    return _inner


async def _finalize_proc(proc: asyncio.subprocess.Process, label: str, timeout: int) -> RunOutput:
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
        kill_process_group(proc)
        try:
            await proc.wait()
        except ProcessLookupError:
            pass
        return RunOutput(
            command=label,
            exit_code=-1,
            output=f"（執行超過 {timeout} 秒逾時，已中止）",
            timed_out=True,
        )
    except asyncio.CancelledError:
        # 外層（如 OpenAIExpert 整輪 hard-timeout 守衛）取消本協程時，asyncio.TimeoutError
        # 之外另拋 CancelledError，原本不收屍會留下整組子程序孤兒繼續燒額度／CPU。對整組
        # killpg 後原樣 re-raise，維持取消語義。
        kill_process_group(proc)
        raise


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
    # 資源上限經 fork-exec 繼承：沙箱時設在 bwrap 進程上（傳入沙箱子進程），非沙箱時直接設在 sh。
    preexec = _rlimit_preexec()
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
            start_new_session=True,
            preexec_fn=preexec,
        )
    else:
        proc = await asyncio.create_subprocess_shell(
            inner,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
            preexec_fn=preexec,
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
            start_new_session=True,
        )
    else:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
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
    """決定 Demo 要跑什麼：優先用宣告的執行指令，否則偵測入口跑 python3。"""
    if declared:
        return declared
    entry = detect_entrypoint(cwd)
    return f"python3 {entry}" if entry else None


def parse_demo_url(text: str) -> str | None:
    """從專家文字解析 `Demo 網址: http://localhost:<port>/...`。

    僅放行本機 URL（localhost / 127.0.0.1）——HTTP 驗收只探測自己剛啟動的服務，
    絕不對外部主機發請求。
    """
    m = re.search(r"(?:Demo ?網址|demo ?url)\s*[:：]\s*(\S+)", text, re.I)
    if not m:
        return None
    url = m.group(1).strip().strip("`")
    if re.match(r"^https?://(localhost|127\.0\.0\.1)(:\d+)?(/|$)", url):
        return url
    return None


def _http_get(url: str, timeout: float = 3.0) -> tuple[int | None, str]:
    """同步 GET（給 to_thread 用）：回 (狀態碼, 內容片段)；連不上回 (None, "")。"""
    import urllib.error
    import urllib.request

    try:
        # nosec B310 — url 已由 parse_demo_url 限定 localhost/127.0.0.1
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310
            return resp.status, resp.read(2048).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, ""  # 4xx/5xx＝服務有回應，狀態碼交由呼叫端裁決
    except OSError:
        return None, ""


async def run_http_demo(
    cwd: Path | str,
    command: str,
    url: str,
    timeout: int | None = None,
    sandbox: bool | None = None,
) -> tuple[RunOutput, int | None]:
    """網站/服務的 HTTP 驗收：啟動服務 → 輪詢 url 至就緒 → GET 取狀態碼與內容 → 收掉服務。

    解決「執行指令是常駐 server」時純 run_command 只能等逾時、無從驗證的缺口。
    服務仍跑在沙箱（PID 隔離、host 唯讀）但「不」斷網（net=True，與 host 共享 loopback，
    否則探測不到）；url 僅限本機（parse_demo_url 已過濾）。
    回傳 (RunOutput, 狀態碼)；ok ＝ 時限內就緒且狀態碼 < 500。
    """
    timeout = timeout or config.DEMO_TIMEOUT
    use_sandbox = config.SANDBOX_ENABLED if sandbox is None else sandbox
    inner = _executable_command(command)
    preexec = _rlimit_preexec()
    display = f"{command} ⇒ GET {url}"
    if use_sandbox:
        if not config._sandbox_available():
            return _sandbox_blocked(display), None
        proc = await asyncio.create_subprocess_exec(
            *_bwrap_prefix(cwd, net=True),
            "/bin/sh",
            "-c",
            inner,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
            preexec_fn=preexec,
        )
    else:
        proc = await asyncio.create_subprocess_shell(
            inner,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
            preexec_fn=preexec,
        )

    # 並行讀走服務輸出，避免 server 寫滿 pipe buffer 被 block。
    chunks: list[bytes] = []

    async def _drain() -> None:
        try:
            while True:
                blob = await proc.stdout.read(4096)
                if not blob:
                    return
                chunks.append(blob)
        except (OSError, ValueError):
            return

    drain_task = asyncio.create_task(_drain())
    status: int | None = None
    body = ""
    early_exit_code: int | None = None  # 服務在探測成功前就自行退出（崩潰／port 被占）
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    try:
        while loop.time() < deadline:
            if proc.returncode is not None:
                early_exit_code = proc.returncode
                break  # 就緒無望
            status, body = await asyncio.to_thread(_http_get, url)
            if status is not None:
                break
            await asyncio.sleep(0.5)
    finally:
        if proc.returncode is None:
            kill_process_group(proc)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(proc.wait(), timeout=10)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(drain_task, timeout=5)

    server_out = b"".join(chunks).decode("utf-8", "replace")
    ok = status is not None and status < 500
    if status is not None:
        probe = f"GET {url} → HTTP {status}"
    elif early_exit_code is not None:
        probe = f"服務啟動後即退出（exit={early_exit_code}），未能回應 {url}"
    else:
        probe = f"服務在 {timeout}s 內未就緒，{url} 無回應"
    parts = [probe]
    if body.strip():
        parts.append(f"--- 回應內容（截斷） ---\n{body.strip()}")
    if server_out.strip():
        parts.append(f"--- 服務輸出 ---\n{server_out.strip()}")
    result = RunOutput(
        command=display,
        exit_code=0 if ok else 1,
        output=_truncate("\n".join(parts)),
        timed_out=status is None and early_exit_code is None,
    )
    return result, status


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
    # 全程走參數式 exec：固定 git 指令拆成 argv，shell 不參與解析（防注入）。
    # sandbox 顯式帶 False（run_command_exec 預設 None 會走 fail-closed）。
    r = await run_command_exec(root, ["git", "init", "-q"], timeout=20, sandbox=False)
    if not r.ok:
        return False
    await run_command_exec(
        root, ["git", "config", "user.email", "studio@ti.local"], timeout=20, sandbox=False
    )
    # 值為 "Ti Studio"（不帶單引號——引號是 shell 產物，argv 不需要）。
    await run_command_exec(
        root, ["git", "config", "user.name", "Ti Studio"], timeout=20, sandbox=False
    )
    # 關閉 commit 簽章，避免外部簽章環境導致 workspace commit 失敗。
    await run_command_exec(
        root, ["git", "config", "commit.gpgsign", "false"], timeout=20, sandbox=False
    )
    return True


# --- clone 既有 GitHub 倉庫到 workspace --------------------------------

# 僅接受 github.com 的 https 倉庫網址（避免任意主機 / 路徑，降低風險）。
_REPO_RE = re.compile(r"^https://github\.com/[\w.-]+/[\w.-]+?(?:\.git)?/?$", re.I)
# 首字元不允許 '-'：避免 `--foo` / `-o` 這類「以選項開頭」的 branch 通過過濾，
# 進而在 `git clone ... --branch <branch>` 被 git 當成參數注入（如 --upload-pack）。
_BRANCH_RE = re.compile(r"^[\w./][\w./-]{0,199}$")

# git merge 在「還沒開始合併」就被工作樹擋下的訊息（未追蹤檔會被覆寫／有未提交本地修改）。
# 這些不含 "CONFLICT"，故與內容衝突分開辨識；皆屬可復原（序列化重跑），不該當硬失敗。
_MERGE_BLOCKED_RE = re.compile(
    r"untracked working tree files would be overwritten"
    r"|local changes to the following files would be overwritten"
    r"|Please (commit your changes or stash|move or remove)",
    re.IGNORECASE,
)


def is_valid_repo_url(url: str) -> bool:
    return bool(_REPO_RE.match((url or "").strip()))


def build_clone_url(url: str, token: str | None) -> str:
    """私有倉庫時把 token 注入 https URL；否則原樣回傳。"""
    url = (url or "").strip()
    if token and url.startswith("https://github.com/"):
        return url.replace("https://", f"https://x-access-token:{token}@", 1)
    return url


async def git_clone(
    url: str,
    dest: Path | str,
    token: str | None = None,
    branch: str | None = None,
    depth: int | None = 1,
) -> RunOutput:
    """把 GitHub 倉庫 clone 到（空的）dest 目錄。回傳 RunOutput（output 已遮蔽 token）。

    depth 預設 1（一次性 session 只需最新快照）；長期專案要拿該 repo 當工作基底時傳
    None 做完整 clone——跨場次的快轉判定（merge-base --is-ancestor）在 shallow
    邊界上會失真，且專家需要能讀完整 git log。
    """
    if not _git_available():
        return RunOutput("git clone", -1, "（環境沒有 git，無法 clone）", False)
    authed = build_clone_url(url, token)
    parts = ["git", "clone"] + (["--depth", str(depth)] if depth else [])
    if branch and _BRANCH_RE.match(branch):
        parts += ["--branch", branch]
    # 直接組 argv 走 exec，shell 不參與解析（authed url / branch 一律當純文字）。
    # label 固定為 "git clone"，嚴禁帶含 token 的 authed，避免 token 寫進日誌。
    argv = parts + [authed, "."]
    result = await run_command_exec(dest, argv, timeout=180, sandbox=False, label="git clone")
    # 避免 token 出現在回報的指令 / 輸出裡（保持在遮蔽之後、return 之前）
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
        log.warning(
            "git add 失敗（exit=%s, timed_out=%s）：%s", add.exit_code, add.timed_out, add.output
        )
        return None

    # commit 直接帶 git identity 兜底，消滅 clone 流程下 identity 缺失整類失敗。
    r = await run_command_exec(
        root,
        [
            "git",
            "-c",
            f"user.name={_GIT_USER_NAME}",
            "-c",
            f"user.email={_GIT_USER_EMAIL}",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-q",
            "-m",
            message,
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


# --- git worktree（並行支線隔離）--------------------------------------
# 每條並行 lane 在 workspace 外的兄弟目錄開獨立 worktree + 分支，各自 commit、互不干擾主
# repo 的 index；完工後依序 merge 回主分支。worktree 路徑在 workspace 沙箱外，故這些 git
# 基礎操作一律 sandbox=False（與 git_init 同款，非 agent 指令、為確定性基礎建設）。


@dataclass
class MergeResult:
    """git merge 結果。

    conflict=True：內容合併衝突（同檔雙方都改），呼叫端應 abort 後走「lane 內解衝突／
    序列化重跑」fallback。
    blocked=True：合併還沒開始就被工作樹擋下（主工作樹有未追蹤檔會被覆寫，或有未提交的
    本地修改），git 連 merge 都不啟動、無 MERGE_HEAD 可 abort。這同樣是「可復原」失敗
    （序列化重跑會在主工作樹就地把既有檔案 commit 進來），呼叫端不該當成未知硬失敗而把
    lane 成果丟掉。這類訊息不含 "CONFLICT" 字樣，過去被誤判為硬失敗、靜默吞掉並行成果。
    """

    ok: bool
    conflict: bool
    output: str
    blocked: bool = False


async def git_worktree_add(
    repo: Path | str, worktree_path: Path | str, branch: str, base: str = "HEAD"
) -> bool:
    """在 repo 上開新分支 <branch> 並 checkout 到獨立 worktree 目錄。回傳是否成功。

    branch 以 _BRANCH_RE 驗證，擋下以選項開頭（如 --upload-pack）的注入。repo 尚無 .git
    時先 git_init（但此時通常已有 PM 規劃的首個 commit，base=HEAD 有效）。
    """
    if not config.ENABLE_GIT or not _git_available():
        return False
    if not _BRANCH_RE.match(branch):
        log.warning("git worktree add 被拒：不合法的 branch 名 %r", branch)
        return False
    root = Path(repo)
    if not (root / ".git").exists() and not await git_init(root):
        return False
    # 開分支前先清掉前一輪 timeout/被 kill 沒 teardown 的殘留：prune 失聯的 worktree
    # 註冊，再強刪可能殘存的同名分支——否則 `worktree add -b` 會以「branch already
    # exists」exit 255，整條 lane 開不起來（歷史上 task-1 反覆撞名即此因）。
    await run_command_exec(
        root, ["git", "worktree", "prune"], timeout=30, sandbox=False, label="git worktree prune"
    )
    await run_command_exec(
        root, ["git", "branch", "-D", branch], timeout=30, sandbox=False, label="git branch -D 殘留"
    )
    r = await run_command_exec(
        root,
        ["git", "worktree", "add", "-b", branch, str(worktree_path), base],
        timeout=60,
        sandbox=False,
        label="git worktree add",
    )
    if not r.ok:
        log.warning("git worktree add 失敗（exit=%s）：%s", r.exit_code, r.output)
    return r.ok


async def git_ensure_initial_commit(repo: Path | str) -> str | None:
    """確保 repo 至少有一個 commit（並行 worktree 需要可分支的 base）。回傳 HEAD 短 hash。

    已有 commit 直接回傳其短 hash；HEAD 未誕生（剛 init、PM 規劃無實質檔案）時建一個空的
    初始 commit。帶 git identity + gpgsign=false（與 git_commit 一致）。git 不可用回 None。
    """
    if not config.ENABLE_GIT or not _git_available():
        return None
    root = Path(repo)
    if not (root / ".git").exists() and not await git_init(root):
        return None
    h = await git_head_short(root)
    if h:
        return h
    r = await run_command_exec(
        root,
        [
            "git",
            "-c",
            f"user.name={_GIT_USER_NAME}",
            "-c",
            f"user.email={_GIT_USER_EMAIL}",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "--allow-empty",
            "-q",
            "-m",
            "init",
        ],
        timeout=30,
        sandbox=False,
        label="git commit",
    )
    if not r.ok:
        return None
    return await git_head_short(root)


async def git_head_short(repo: Path | str) -> str | None:
    """回傳 repo 當前 HEAD 的短 hash（失敗回 None）。合併後更新 last_commit 用。"""
    if not _git_available():
        return None
    r = await run_command_exec(
        Path(repo),
        ["git", "rev-parse", "--short", "HEAD"],
        timeout=20,
        sandbox=False,
        label="git rev-parse",
    )
    return r.output.strip() if r.ok else None


# 發佈前淨化用:絕不該交付的環境/沙箱/快取產物。baseline .gitignore 樣式 + 已追蹤時 untrack 路徑。
_BASELINE_IGNORE_PATTERNS = [
    "__pycache__/",
    "*.py[cod]",
    "*.egg-info/",
    ".eggs/",
    "build/",
    "dist/",
    ".Python",
    ".venv/",
    "venv/",
    "env/",
    "ENV/",
    "*.db",
    "*.db-shm",
    "*.db-wal",
    "*.sqlite",
    "*.sqlite3",
    ".env",
    "*.env.local",
    # .idea/.vscode 用無斜線形式:沙箱可能建「同名 0-byte 檔」,目錄式 `.idea/` 擋不到檔
    ".idea",
    ".vscode",
    # SDK 專家沙箱會把 HOME/設定 dotfiles 散落進 workspace（皆非專案內容,絕不交付）
    ".claude/",
    ".bashrc",
    ".bash_profile",
    ".profile",
    ".zshrc",
    ".zprofile",
    ".gitconfig",
    ".gitmodules",
    ".ripgreprc",
    ".mcp.json",
]
# 已被早期 `git add -A` 追蹤的 junk（.gitignore 擋不到已追蹤檔,須顯式 untrack）。
_JUNK_PATHS = [
    ".venv",
    "venv",
    "env",
    "ENV",
    ".claude",
    ".idea",
    ".vscode",
    "__pycache__",
    "dist",
    "build",
    ".eggs",
    ".bashrc",
    ".bash_profile",
    ".profile",
    ".zshrc",
    ".zprofile",
    ".gitconfig",
    ".gitmodules",
    ".ripgreprc",
    ".mcp.json",
    "*.db",
    "*.db-shm",
    "*.db-wal",
    "*.pyc",
    "*.egg-info",
]


def write_baseline_gitignore(cwd: Path | str) -> None:
    """把 baseline 忽略樣式併入 cwd/.gitignore（保留專案既有內容,只補缺的）。純檔案寫入、
    不需 .git——故可在 session 一開始（任何 commit 之前）就呼叫,讓 SDK 沙箱散落的 dotfiles／
    .venv／*.db 等 junk **從不被 `git add -A` 追蹤**（乾淨歷史＋乾淨 lane 分支,減少合併摩擦）。
    與 git_sanitize_workspace（發佈前對「已追蹤」junk 兜底 untrack）互補。失敗吞掉不拋。"""
    try:
        root = Path(cwd)
        if not root.is_dir():
            return
        gi = root / ".gitignore"
        existing = gi.read_text(encoding="utf-8", errors="replace") if gi.exists() else ""
        have = {ln.strip() for ln in existing.splitlines()}
        missing = [p for p in _BASELINE_IGNORE_PATTERNS if p not in have]
        if missing:
            block = (
                "\n# --- Ti baseline（自動補上,避免追蹤/交付沙箱/環境 junk）---\n"
                + "\n".join(missing)
                + "\n"
            )
            gi.write_text(
                (existing.rstrip("\n") + "\n" if existing else "") + block, encoding="utf-8"
            )
    except OSError:
        log.warning("寫入 baseline .gitignore 失敗（略過）", exc_info=True)


async def git_sanitize_workspace(repo: Path | str) -> None:
    """發佈前淨化 workspace,避免交付被沙箱/環境污染的 repo（.venv／*.db／HOME dotfiles／
    .claude 等,實測曾使交付 repo 膨脹到 2000+ 檔)。

    兩步:① 把 baseline 忽略樣式併入 .gitignore（保留專案既有內容,只補缺的,讓後續 `git add`
    不再收 junk）;② 對「已被追蹤」的 junk 顯式 `git rm -r --cached`（.gitignore 只擋未追蹤檔,
    junk 一旦被早期 commit 追蹤就得顯式移除）。最終發佈 commit 即不含這些 junk。任何失敗都
    吞掉不拋(淨化不可拖垮發佈)。應在「發佈前的最後一次 commit」之前呼叫。"""
    if not config.ENABLE_GIT or not _git_available():
        return
    root = Path(repo)
    if not (root / ".git").exists():
        return
    write_baseline_gitignore(root)
    # 已追蹤的 junk → untrack（--ignore-unmatch:沒中也不報錯;-r:含目錄如 .venv/.claude）
    await run_command_exec(
        root,
        ["git", "rm", "-r", "--cached", "--ignore-unmatch", "--", *_JUNK_PATHS],
        timeout=60,
        sandbox=False,
        label="git rm --cached junk",
    )


async def git_has_changes(repo: Path | str) -> bool:
    """工作樹是否有未提交變更（含未追蹤檔）。用於偵測「工程師那輪聲稱寫檔卻零變更」的
    幻覺寫檔——`git status --porcelain` 有任何輸出即 True。git 不可用／查詢失敗一律回 False
    （保守:無法確認就不誤判幻覺,避免對正常流程注入錯誤糾正）。"""
    if not config.ENABLE_GIT or not _git_available():
        return False
    r = await run_command_exec(
        Path(repo),
        ["git", "status", "--porcelain"],
        timeout=20,
        sandbox=False,
        label="git status",
    )
    return bool(r.ok and r.output.strip())


async def git_current_branch(repo: Path | str) -> str | None:
    """回傳 repo 目前所在分支名（detached / 失敗回 None）。merge 目標即主分支。"""
    if not _git_available():
        return None
    r = await run_command_exec(
        Path(repo),
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        timeout=20,
        sandbox=False,
        label="git rev-parse",
    )
    name = r.output.strip() if r.ok else ""
    return name or None


async def git_merge_worktree(repo: Path | str, branch: str) -> MergeResult:
    """在主 repo 把 lane 分支 <branch> 以 --no-ff 合併回當前分支。

    回傳 MergeResult。衝突（exit!=0 且輸出含 CONFLICT / Automatic merge failed）時
    ok=False、conflict=True，呼叫端應 git_merge_abort 後走序列化重跑 fallback。
    合併會建 merge commit，故帶 git identity 與 gpgsign=false（與 git_commit 一致）。
    """
    if not config.ENABLE_GIT or not _git_available():
        return MergeResult(ok=False, conflict=False, output="（git 不可用）")
    if not _BRANCH_RE.match(branch):
        return MergeResult(ok=False, conflict=False, output=f"不合法的 branch 名：{branch!r}")
    r = await run_command_exec(
        Path(repo),
        [
            "git",
            "-c",
            f"user.name={_GIT_USER_NAME}",
            "-c",
            f"user.email={_GIT_USER_EMAIL}",
            "-c",
            "commit.gpgsign=false",
            "merge",
            "--no-ff",
            "--no-edit",
            branch,
        ],
        timeout=60,
        sandbox=False,
        label="git merge",
    )
    if r.ok:
        return MergeResult(ok=True, conflict=False, output=r.output)
    conflict = bool(re.search(r"CONFLICT|Automatic merge failed", r.output))
    blocked = not conflict and bool(_MERGE_BLOCKED_RE.search(r.output))
    return MergeResult(ok=False, conflict=conflict, output=r.output, blocked=blocked)


async def git_merge_abort(repo: Path | str) -> None:
    """中止進行中的 merge，把主 repo working tree 還原乾淨（衝突 fallback 用）。"""
    if not _git_available():
        return
    await run_command_exec(
        Path(repo),
        ["git", "merge", "--abort"],
        timeout=20,
        sandbox=False,
        label="git merge --abort",
    )


async def git_merge_ref_into(repo: Path | str, ref: str) -> MergeResult:
    """在 `repo`（通常是 lane 的 worktree）把 <ref> 合併進當前分支，保留衝突標記不自動 abort。

    用於「lane 內解衝突」：把最新主幹（ref＝主幹 HEAD 短 hash）merge 進 lane 分支，衝突時
    讓 working tree 留著 `<<<<<<<` 標記，交由該 lane 的工程師就地解決後再 commit、合回主幹。
    ref 以 `_BRANCH_RE` 驗證（短 hash 亦符合），擋下以選項開頭的注入。
    """
    if not config.ENABLE_GIT or not _git_available():
        return MergeResult(ok=False, conflict=False, output="（git 不可用）")
    if not _BRANCH_RE.match(ref):
        return MergeResult(ok=False, conflict=False, output=f"不合法的 ref：{ref!r}")
    r = await run_command_exec(
        Path(repo),
        [
            "git",
            "-c",
            f"user.name={_GIT_USER_NAME}",
            "-c",
            f"user.email={_GIT_USER_EMAIL}",
            "-c",
            "commit.gpgsign=false",
            "merge",
            "--no-edit",
            ref,
        ],
        timeout=60,
        sandbox=False,
        label="git merge",
    )
    if r.ok:
        return MergeResult(ok=True, conflict=False, output=r.output)
    conflict = bool(re.search(r"CONFLICT|Automatic merge failed", r.output))
    blocked = not conflict and bool(_MERGE_BLOCKED_RE.search(r.output))
    return MergeResult(ok=False, conflict=conflict, output=r.output, blocked=blocked)


async def git_conflict_markers_present(repo: Path | str) -> bool:
    """working tree 是否仍殘留未解的衝突標記（`<<<<<<<` 等）。

    以 `git diff --check` 偵測：有殘留衝突標記時輸出含 "conflict marker" 且 exit≠0。
    無法判定（git 不可用）時保守回 True（視為未解 → 呼叫端走安全 fallback）。
    """
    if not _git_available():
        return True
    r = await run_command_exec(
        Path(repo),
        ["git", "diff", "--check"],
        timeout=20,
        sandbox=False,
        label="git diff --check",
    )
    return "conflict marker" in r.output.lower()


async def git_worktree_remove(
    repo: Path | str, worktree_path: Path | str, branch: str | None = None
) -> None:
    """移除 lane 的 worktree（--force 容忍未清理變更），並盡力刪除其分支（best-effort）。"""
    if not _git_available():
        return
    root = Path(repo)
    await run_command_exec(
        root,
        ["git", "worktree", "remove", "--force", str(worktree_path)],
        timeout=30,
        sandbox=False,
        label="git worktree remove",
    )
    if branch and _BRANCH_RE.match(branch):
        await run_command_exec(
            root,
            ["git", "branch", "-D", branch],
            timeout=20,
            sandbox=False,
            label="git branch -D",
        )
