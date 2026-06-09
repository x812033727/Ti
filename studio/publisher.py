"""把工作室成果（workspace 內的獨立 git repo）發佈到 GitHub。

對外動作，預設關閉：需設定 `GITHUB_TOKEN` 與 `TI_PUBLISH_REPO`（owner/repo）才會啟用。
流程：在 workspace repo 建立分支 → 加上帶 token 的 remote → push → 視設定開 PR。
純邏輯（分支命名 / URL 組裝 / token 遮蔽 / PR payload）與實際 IO 分離，方便單元測試。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass

from . import config, runner

# gh 從環境（HOME=/root 的 ~/.config/gh）讀 token；輸出不含 token，但對外回傳仍經 redact() 防呆。
_GH = ["gh"]


@dataclass
class PublishResult:
    ok: bool
    detail: str = ""
    branch: str = ""
    repo: str | None = None
    pushed: bool = False
    pr_url: str | None = None

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "detail": self.detail,
            "branch": self.branch,
            "repo": self.repo,
            "pushed": self.pushed,
            "pr_url": self.pr_url,
        }


# --- 純邏輯（可單測）---------------------------------------------------


def is_configured() -> bool:
    return bool(config.GITHUB_TOKEN and config.PUBLISH_REPO)


def branch_name(session_id: str) -> str:
    safe = "".join(c for c in session_id if c.isalnum() or c in "-_") or "session"
    return f"ti-studio/{safe}"


def remote_url(repo: str, token: str) -> str:
    return f"https://x-access-token:{token}@github.com/{repo}.git"


def redact(text: str, token: str | None = None) -> str:
    token = token or config.GITHUB_TOKEN
    if token and text:
        text = text.replace(token, "***")
    return text


def pr_payload(requirement: str, branch: str, base: str) -> dict:
    title = "Ti Studio 成果：" + (requirement or "").strip()[:60]
    body = (
        f"此 PR 由 Ti Studio AI 專家工作室自動產生。\n\n**原始需求**：{requirement or '(未提供)'}\n"
    )
    return {"title": title, "head": branch, "base": base, "body": body}


# --- 實際發佈 ----------------------------------------------------------


async def _push(cwd, branch: str, url: str) -> runner.RunOutput:
    # 全程走參數式 exec：branch/url 當單一 argv，免 shell 解析（防注入）；
    # 且用簡短 label，避免帶 token 的 remote url 出現在 RunOutput.command。
    await runner.run_command_exec(
        cwd, ["git", "branch", "-M", branch], timeout=30, sandbox=False, label="git branch"
    )
    await runner.run_command_exec(
        cwd,
        ["git", "remote", "remove", "ti_publish"],
        timeout=20,
        sandbox=False,
        label="git remote remove",
    )
    await runner.run_command_exec(
        cwd,
        ["git", "remote", "add", "ti_publish", url],
        timeout=20,
        sandbox=False,
        label="git remote add",
    )
    return await runner.run_command_exec(
        cwd,
        ["git", "push", "-u", "ti_publish", branch],
        timeout=120,
        sandbox=False,
        label="git push",
    )


async def _open_pr(payload: dict) -> tuple[bool, str]:
    """呼叫 GitHub REST 建 PR；回傳 (是否成功, url 或錯誤訊息)。"""
    import httpx

    url = f"https://api.github.com/repos/{config.PUBLISH_REPO}/pulls"
    headers = {
        "Authorization": f"Bearer {config.GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
    }
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(url, json=payload, headers=headers)
    if r.status_code in (200, 201):
        return True, r.json().get("html_url", "")
    return False, f"PR 建立失敗（{r.status_code}）：{r.text[:200]}"


async def publish(cwd, session_id: str, requirement: str, *, make_pr: bool = True) -> PublishResult:
    if not is_configured():
        return PublishResult(False, "未設定 GITHUB_TOKEN 或 TI_PUBLISH_REPO，無法發佈")

    repo = config.PUBLISH_REPO
    branch = branch_name(session_id)

    # 確保有 git repo 與至少一個 commit
    await runner.git_init(cwd)
    await runner.git_commit(cwd, "Ti Studio 成果")

    push = await _push(cwd, branch, remote_url(repo, config.GITHUB_TOKEN))
    if not push.ok:
        return PublishResult(False, "push 失敗：" + redact(push.output), branch=branch, repo=repo)

    if not make_pr:
        return PublishResult(True, "已 push", branch=branch, repo=repo, pushed=True)

    ok, info = await _open_pr(pr_payload(requirement, branch, config.PUBLISH_BASE))
    if ok:
        return PublishResult(
            True, "已 push 並建立 PR", branch=branch, repo=repo, pushed=True, pr_url=info
        )
    return PublishResult(True, "已 push，但 " + info, branch=branch, repo=repo, pushed=True)


# --- CI/CD 驗證 + 自動合併（對外、走 gh）-------------------------------
# 全部 sandbox=False（要連外）、簡短 label（避免污染日誌），ref 一律用分支名當 PR selector。


def _no_checks(output: str) -> bool:
    """gh 在「該分支沒有任何 check」時的輸出特徵；用文字判定比依賴 exit code 穩。"""
    return "no checks reported" in (output or "").lower()


async def check_ci(repo: str, ref: str, *, grace: int | None = None, timeout: int | None = None):
    """驗證 ref（分支）對應 PR 的 CI/CD 狀態。

    回傳 (state, detail)，state ∈ {"pass","fail","none","error"}：
      pass  — 所有 check 通過
      fail  — 有 check 失敗
      none  — 寬限期過後仍無任何 check（視同無 CI）
      error — 等待逾時或 gh 本身出錯
    """
    grace = config.PUBLISH_CI_GRACE if grace is None else grace
    timeout = config.PUBLISH_CI_TIMEOUT if timeout is None else timeout

    # 1) 寬限輪詢：等 push 後 check 註冊出現（CI 常有數十秒延遲）。
    waited = 0
    interval = 15
    has_checks = False
    while waited < grace:
        r = await runner.run_command_exec(
            cwd=".", argv=[*_GH, "pr", "checks", ref, "-R", repo], timeout=60,
            sandbox=False, label="gh pr checks",
        )
        if r.ok:
            return "pass", redact(r.output[-500:])
        if not _no_checks(r.output):
            # check 已存在（pending/部分失敗）→ 進入等完成階段。
            has_checks = True
            break
        await asyncio.sleep(interval)
        waited += interval
    if not has_checks:
        return "none", "寬限期內未偵測到任何 CI check，視同無 CI"

    # 2) 等所有 check 完成（--watch；--fail-fast 一失敗即返回）。
    r = await runner.run_command_exec(
        cwd=".",
        argv=[*_GH, "pr", "checks", ref, "-R", repo, "--watch", "--fail-fast", "--interval", "15"],
        timeout=timeout, sandbox=False, label="gh pr checks --watch",
    )
    if r.timed_out:
        return "error", f"等待 CI 完成逾時（>{timeout}s）"
    if r.ok:
        return "pass", redact(r.output[-500:])
    if _no_checks(r.output):
        return "none", "無任何 CI check，視同無 CI"
    return "fail", redact(r.output[-1500:])


async def ci_failure_logs(repo: str, branch: str, ref: str) -> str:
    """盡力取最近一次失敗 run 的日誌餵給工程師；取不到就退回 checks 摘要。"""
    listing = await runner.run_command_exec(
        cwd=".",
        argv=[
            *_GH, "run", "list", "-R", repo, "--branch", branch, "-L", "5",
            "--json", "databaseId,conclusion,status,workflowName",
        ],
        timeout=60, sandbox=False, label="gh run list",
    )
    run_id = None
    if listing.ok:
        try:
            for run in json.loads(listing.output or "[]"):
                if run.get("conclusion") == "failure":
                    run_id = run.get("databaseId")
                    break
        except (ValueError, TypeError):
            run_id = None
    if run_id is not None:
        logs = await runner.run_command_exec(
            cwd=".", argv=[*_GH, "run", "view", str(run_id), "-R", repo, "--log-failed"],
            timeout=120, sandbox=False, label="gh run view --log-failed",
        )
        if logs.output.strip():
            return redact(logs.output[-4000:])
    # 退回：用 pr checks 的摘要當線索。
    summary = await runner.run_command_exec(
        cwd=".", argv=[*_GH, "pr", "checks", ref, "-R", repo], timeout=60,
        sandbox=False, label="gh pr checks",
    )
    return redact(summary.output[-2000:]) or "（無法取得失敗日誌）"


async def merge_pr(repo: str, ref: str) -> tuple[bool, str]:
    """squash-merge 並刪除分支；預設不帶 --admin（讓分支保護生效）。"""
    admin = ["--admin"] if config.PUBLISH_MERGE_ADMIN else []
    r = await runner.run_command_exec(
        cwd=".",
        argv=[*_GH, "pr", "merge", ref, "-R", repo, "--squash", *admin, "--delete-branch"],
        timeout=180, sandbox=False, label="gh pr merge",
    )
    if r.ok:
        return True, f"已 squash-merge {ref} 並刪除分支"
    return False, "merge 失敗：" + redact(r.output[-500:])


async def repush(cwd, branch: str) -> runner.RunOutput:
    """把工程師修正後的 commit 重推同一分支（remote ti_publish 由初次 _push 已建好）。"""
    return await runner.run_command_exec(
        cwd, ["git", "push", "ti_publish", branch], timeout=120, sandbox=False, label="git push",
    )
