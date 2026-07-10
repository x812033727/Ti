"""目標 repo＝工作基底＋發佈目標：每場 session 開始前把專案 workspace 同步到該 repo。

專案設定了 publish_repo 後，它不只是「成果推過去開 PR」的發佈目標，更是專家的
工作基底——使用者指定 repo 的本意是「在我的程式碼上做修改」，不是另起爐灶：

  workspace 全新     → 完整 clone 該 repo 的 base 分支當基底（歷史同源，PR 可合併）
  workspace 已同源   → fetch + fast-forward 到遠端 base（吃回上場已合併的 PR）
  workspace 已分歧   → 絕不清空、絕不覆蓋，只回報明確警告（發佈照舊，PR 走既有
                       「無共同歷史」友善訊息）

同步只用「絕不破壞」的操作：clone 進空目錄、ff-only 快轉、同步前先 commit 收掉
未提交變更；全程無 reset --hard（唯一例外：unborn HEAD 且目錄裡只有 .git——
本質上沒有東西可毀）。所有對外輸出（detail／broadcast）一律遮蔽 token。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import config, events, git_cred, runner

# 這些字樣代表「遠端拿不到指定分支」而非本地/憑證問題：repo 不存在、空 repo、
# 或 base 分支不存在。此時從空白開始是預期路徑（首次發佈由 publisher._ensure_repo
# 自動建 repo、_push_base 初始化 base），不是錯誤。
_REMOTE_MISSING_MARKS = (
    "repository not found",
    "not found in upstream",
    "couldn't find remote ref",
    "could not find remote branch",
)

# workspace 內容確實源自目標 repo 的狀態（發佈時歷史同源，PR 可正常合併）。
_BASED_STATUSES = frozenset(
    {"cloned", "fast_forwarded", "up_to_date", "local_ahead", "local_behind", "forked"}
)


@dataclass
class SyncResult:
    """一次基底同步的結果。

    status:
      cloned             全新 workspace 已以目標 repo 為基底（clone 或 unborn 注入）
      fast_forwarded     本地 base 已快轉到遠端（吃回已合併的 PR）
      up_to_date         本地與遠端 base 相同
      local_ahead        本地領先遠端（上場 PR 尚未合併），維持本地
      local_behind       本地落後但無法快轉（如未提交變更擋路），維持本地
      forked             與遠端各自有新 commit 但仍同源（有共同祖先），維持本地
      diverged           與目標 repo 無共同歷史，維持本地（絕不清空）
      remote_unavailable 遠端拿不到 base（repo 不存在/空 repo/網路），從現狀續行
      skipped            未設定 repo／離線／git 停用，未做任何事
      error              全新 workspace 取基底失敗（憑證/網路），應中止場次
    """

    status: str
    detail: str = ""

    @property
    def based(self) -> bool:
        """workspace 內容是否確實源自目標 repo（可據此對專家宣告「既有程式碼在目錄裡」）。"""
        return self.status in _BASED_STATUSES

    @property
    def fatal(self) -> bool:
        return self.status == "error"


def _redact(text: str, token: str | None) -> str:
    # git 失敗訊息會原樣回顯 remote URL（含 x-access-token:<token>@），輸出前必遮。
    if token and text:
        text = text.replace(token, "***")
    return text


async def _git(
    cwd: Path,
    argv: list[str],
    label: str,
    timeout: int = 30,
    env: dict[str, str] | None = None,
) -> runner.RunOutput:
    # 基底同步是確定性基礎建設（非 agent 指令），與 git_init/_push 同款 sandbox=False；
    # label 固定短字串，帶 token 的 URL 絕不進 RunOutput.command。
    kwargs = {"timeout": timeout, "sandbox": False, "label": label}
    if env:
        kwargs["env"] = env
    return await runner.run_command_exec(cwd, argv, **kwargs)


async def workspace_state(path: Path | str) -> str:
    """workspace 現況分類：

    pristine     目錄不存在或全空 → 可安全 clone
    unborn       只有 .git 且 HEAD 尚未誕生（init 過、零 commit、零散檔）
    has_history  HEAD 可解析（已有本地歷史）
    local_files  其餘（有檔案但無 .git、或 unborn 但夾雜散檔）→ 一律不碰
    """
    root = Path(path)
    if not root.is_dir():
        return "pristine"
    entries = list(root.iterdir())
    if not entries:
        return "pristine"
    if not (root / ".git").exists():
        return "local_files"
    head = await _git(root, ["git", "rev-parse", "-q", "--verify", "HEAD"], "git rev-parse", 20)
    if head.ok:
        return "has_history"
    non_git = [p for p in entries if p.name != ".git"]
    return "unborn" if not non_git else "local_files"


async def sync_workspace(
    cwd: Path | str, url: str, base: str, *, token: str | None = None
) -> SyncResult:
    """把 workspace 同步到 url 的 base 分支（狀態機，見模組 docstring）。

    url 顯式注入（不在此處讀 config），測試可用 file:///…/bare.git 走真 git 不碰網路。
    """
    root = Path(cwd)
    state = await workspace_state(root)

    if state == "local_files":
        return SyncResult(
            "diverged",
            "workspace 已有內容但不是 git 歷史（或夾雜未追蹤檔），無法以目標 repo 為基底；"
            "維持本地內容（絕不清空）",
        )

    if state == "pristine":
        root.mkdir(parents=True, exist_ok=True)
        clone = await runner.git_clone(url, root, token=token, branch=base, depth=None)
        if clone.ok:
            return SyncResult("cloned", f"已以 {url} 的 {base} 分支為工作基底")
        out = _redact(clone.output, token)
        if any(mark in out.lower() for mark in _REMOTE_MISSING_MARKS):
            return SyncResult(
                "remote_unavailable",
                f"目標 repo 不存在或沒有 {base} 分支，將從空白開始；"
                "首次發佈時會自動建立 repo／初始化 base 分支",
            )
        # 全新 workspace 拿不到基底時 fail-fast 是安全的：本地還沒有任何東西，
        # 硬上只會重演「無共同歷史」——正是本模組要消滅的結局。
        return SyncResult(
            "error",
            f"無法取得目標 repo 作為工作基底（請確認 GITHUB_TOKEN 權限與網路）：{out[:300]}",
        )

    # unborn / has_history：fetch 遠端 base（fetch 直接用 URL，不持久化帶 token 的 remote）。
    fetch_url = runner.build_clone_url(url, token, legacy=config.TI_GIT_CRED_LEGACY)
    fetch_env = git_cred.make_env(token, url=fetch_url)
    fetch = await _git(
        root,
        ["git", "fetch", fetch_url, f"refs/heads/{base}"],
        "git fetch",
        120,
        env=fetch_env or None,
    )
    if not fetch.ok:
        return SyncResult(
            "remote_unavailable",
            f"無法從目標 repo 取得 {base} 分支（repo 不存在／空 repo／網路問題），"
            "維持本地現狀續行，下場再同步",
        )

    if state == "unborn":
        # 目錄裡只有 .git、零 commit 零散檔——注入遠端 base 等價於 clone，無物可毀。
        r = await _git(root, ["git", "reset", "--hard", "FETCH_HEAD"], "git reset")
        if not r.ok:
            return SyncResult("error", "工作基底注入失敗：" + _redact(r.output, token)[:300])
        await _git(root, ["git", "branch", "-M", base], "git branch")
        return SyncResult("cloned", f"已以 {url} 的 {base} 分支為工作基底")

    # has_history：先收掉上場中斷殘留的未提交變更（無變更時為 no-op），再做快轉判定。
    await runner.git_commit(root, "場次開始：保存未提交變更")
    head = await _git(root, ["git", "rev-parse", "HEAD"], "git rev-parse", 20)
    remote = await _git(root, ["git", "rev-parse", "FETCH_HEAD"], "git rev-parse", 20)
    if not head.ok or not remote.ok:
        return SyncResult("error", "無法解析本地/遠端 HEAD：" + _redact(remote.output, token)[:300])
    if head.output.strip() == remote.output.strip():
        return SyncResult("up_to_date", "")

    behind = await _git(
        root, ["git", "merge-base", "--is-ancestor", "HEAD", "FETCH_HEAD"], "git merge-base", 20
    )
    if behind.ok:
        ff = await _git(root, ["git", "merge", "--ff-only", "FETCH_HEAD"], "git merge", 60)
        if ff.ok:
            # 上場發佈會把分支改名成 ti-studio/<sid>（publisher._push），這裡正規化回 base。
            await _git(root, ["git", "branch", "-M", base], "git branch")
            return SyncResult(
                "fast_forwarded",
                f"已同步目標 repo 的 {base} 分支（上場已合併的成果回到工作基底）",
            )
        # HEAD 是遠端祖先但快轉被拒（如未提交變更 commit 失敗仍擋路）：歷史仍同源，
        # 維持本地照常開工，發佈的 PR 仍可正常合併。
        return SyncResult(
            "local_behind",
            "本地落後遠端 base 但暫時無法快轉（可能有未收乾淨的變更），維持本地續行",
        )

    ahead = await _git(
        root, ["git", "merge-base", "--is-ancestor", "FETCH_HEAD", "HEAD"], "git merge-base", 20
    )
    if ahead.ok:
        return SyncResult(
            "local_ahead",
            "本地領先遠端 base（上場 PR 尚未合併），維持本地；本場成果將一併進入下一個 PR",
        )

    # 兩邊各自有新 commit：只要還有共同祖先就是「分叉」而非「不同源」——
    # 成果 PR 仍可正常開（GitHub 以 merge-base 算 diff），不必嚇唬使用者。
    common = await _git(root, ["git", "merge-base", "HEAD", "FETCH_HEAD"], "git merge-base", 20)
    if common.ok:
        return SyncResult(
            "forked",
            "本地與遠端 base 已分叉（兩邊各自有新 commit，例如 PR 被人工改寫後合併），"
            "維持本地內容；成果 PR 仍可正常建立",
        )

    return SyncResult(
        "diverged",
        "workspace 與目標 repo 無共同歷史（可能在設定目標 repo 前已有獨立歷史）；"
        "維持本地內容（絕不清空）。發佈仍會推分支保存，但 PR 會因無共同歷史而開不成",
    )


async def ensure_base(
    cwd: Path | str, repo: str, *, broadcast=None, session_id: str = ""
) -> SyncResult:
    """呼叫端唯一入口：依專案的目標 repo（owner/repo）同步 workspace，並回播進度。

    短路條件（回 skipped）集中在這裡，讓 ws/improver 無條件呼叫即可：
    未設定 repo／離線示範／git 停用或不可用。
    """
    repo = (repo or "").strip()
    if not repo or config.OFFLINE_MODE or not config.ENABLE_GIT or not runner._git_available():
        return SyncResult("skipped")
    result = await sync_workspace(
        cwd,
        f"https://github.com/{repo}",
        config.PUBLISH_BASE,
        token=config.GITHUB_TOKEN or None,
    )
    # up_to_date／skipped 不播（每場都會發生，純噪音）；其餘狀態使用者需要知道。
    if broadcast is not None and result.detail:
        await broadcast(events.phase_change(session_id, "準備", result.detail))
    return result
