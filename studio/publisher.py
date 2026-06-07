"""把工作室成果（workspace 內的獨立 git repo）發佈到 GitHub。

對外動作，預設關閉：需設定 `GITHUB_TOKEN` 與 `TI_PUBLISH_REPO`（owner/repo）才會啟用。
流程：在 workspace repo 建立分支 → 加上帶 token 的 remote → push → 視設定開 PR。
純邏輯（分支命名 / URL 組裝 / token 遮蔽 / PR payload）與實際 IO 分離，方便單元測試。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from . import config, runner

_PR_NUM_RE = re.compile(r"/pull/(\d+)")


@dataclass
class PublishResult:
    ok: bool
    detail: str = ""
    branch: str = ""
    repo: str | None = None
    pushed: bool = False
    pr_url: str | None = None
    pr_number: int | None = None
    merged: bool = False

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "detail": self.detail,
            "branch": self.branch,
            "repo": self.repo,
            "pushed": self.pushed,
            "pr_url": self.pr_url,
            "pr_number": self.pr_number,
            "merged": self.merged,
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


def parse_pr_number(url: str | None) -> int | None:
    """從 PR 的 html_url（…/pull/123）解析出 PR 編號；解析不到回 None。"""
    if not url:
        return None
    m = _PR_NUM_RE.search(url)
    return int(m.group(1)) if m else None


def merge_payload(branch: str, method: str = "merge") -> dict:
    return {
        "commit_title": f"Merge {branch} (Ti Studio)",
        "merge_method": method,
    }


# --- 實際發佈 ----------------------------------------------------------


async def _push(cwd, branch: str, url: str) -> runner.RunOutput:
    await runner.run_command(cwd, f"git branch -M {branch}", timeout=30)
    await runner.run_command(cwd, "git remote remove ti_publish", timeout=20)
    await runner.run_command(cwd, f"git remote add ti_publish {url}", timeout=20)
    return await runner.run_command(cwd, f"git push -u ti_publish {branch}", timeout=120)


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


async def _merge_pr(number: int, payload: dict) -> tuple[bool, str]:
    """呼叫 GitHub REST 合併指定 PR；回傳 (是否成功, sha 或錯誤訊息)。

    不丟例外：衝突（405 不可合併 / 409 SHA 不符）或其他失敗皆回 (False, 錯誤訊息)。
    """
    import httpx

    url = f"https://api.github.com/repos/{config.PUBLISH_REPO}/pulls/{number}/merge"
    headers = {
        "Authorization": f"Bearer {config.GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
    }
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.put(url, json=payload, headers=headers)
    except Exception as e:  # 網路等例外也不外拋，轉成可讀錯誤
        return False, f"merge 請求失敗：{type(e).__name__}"
    if r.status_code == 200:
        return True, r.json().get("sha", "")
    if r.status_code in (405, 409):
        return False, f"merge 衝突／不可合併（{r.status_code}）：{r.text[:200]}"
    return False, f"merge 失敗（{r.status_code}）：{r.text[:200]}"


async def publish(
    cwd, session_id: str, requirement: str, *, make_pr: bool = True, merge: bool = False
) -> PublishResult:
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
    if not ok:
        return PublishResult(
            True, "已 push，但 " + redact(info), branch=branch, repo=repo, pushed=True
        )

    res = PublishResult(
        True,
        "已 push 並建立 PR",
        branch=branch,
        repo=repo,
        pushed=True,
        pr_url=info,
        pr_number=parse_pr_number(info),
    )
    if not merge:
        return res

    # 自動合併（TI_PUBLISH_MERGE 開啟時）。任何失敗皆不丟例外，回 merged=False。
    if res.pr_number is None:
        res.detail = "已 push 並建立 PR，但無法解析 PR 編號，未自動合併"
        return res
    mok, minfo = await _merge_pr(res.pr_number, merge_payload(branch))
    if mok:
        res.merged = True
        res.detail = "已 push、建立 PR 並合併"
    else:
        res.detail = "已 push 並建立 PR，但 " + redact(minfo)
    return res
