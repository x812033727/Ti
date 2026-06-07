"""把工作室成果（workspace 內的獨立 git repo）發佈到 GitHub。

對外動作，預設關閉：需設定 `GITHUB_TOKEN` 與 `TI_PUBLISH_REPO`（owner/repo）才會啟用。
流程：在 workspace repo 建立分支 → 加上帶 token 的 remote → push → 視設定開 PR →（選用）自動 merge。
純邏輯（分支命名 / URL 組裝 / token 遮蔽 / PR / merge payload）與實際 IO 分離，方便單元測試。

`ok` 語意固定為「push 是否成功」：PR 或 merge 失敗不會翻成 ok=False，只透過
`pr_url` / `merged` / `detail` 欄位表達，維持與既有行為一致、向後相容。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from . import config, runner

# merge 前輪詢 PR mergeable 狀態的重試次數與間隔（GitHub 剛建 PR 時 mergeable 常為 null）。
_MERGE_POLL_TRIES = 5
_MERGE_POLL_DELAY = 2.0


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
    merge_sha: str | None = None

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
            "merge_sha": self.merge_sha,
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


def merge_payload(requirement: str, method: str = "merge") -> dict:
    """組裝 PUT /pulls/{n}/merge 的 body。method: merge | squash | rebase。"""
    if method not in ("merge", "squash", "rebase"):
        method = "merge"
    title = "Ti Studio 自動合併：" + (requirement or "").strip()[:60]
    return {"merge_method": method, "commit_title": title}


# --- 實際發佈 ----------------------------------------------------------


async def _push(cwd, branch: str, url: str) -> runner.RunOutput:
    await runner.run_command(cwd, f"git branch -M {branch}", timeout=30)
    await runner.run_command(cwd, "git remote remove ti_publish", timeout=20)
    await runner.run_command(cwd, f"git remote add ti_publish {url}", timeout=20)
    return await runner.run_command(cwd, f"git push -u ti_publish {branch}", timeout=120)


def _gh_headers() -> dict:
    return {
        "Authorization": f"Bearer {config.GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
    }


async def _open_pr(payload: dict) -> tuple[bool, str, int | None]:
    """呼叫 GitHub REST 建 PR；回傳 (是否成功, url 或錯誤訊息, PR number)。"""
    import httpx

    url = f"https://api.github.com/repos/{config.PUBLISH_REPO}/pulls"
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(url, json=payload, headers=_gh_headers())
    if r.status_code in (200, 201):
        data = r.json()
        return True, data.get("html_url", ""), data.get("number")
    return False, f"PR 建立失敗（{r.status_code}）：{redact(r.text[:200])}", None


async def _merge_pr(pr_number: int, payload: dict) -> tuple[bool, str, str | None]:
    """自動合併 PR；回傳 (是否成功, 訊息, merge commit sha)。

    剛建立的 PR `mergeable` 常為 null，GitHub 會回 405；先輪詢/重試數次。
    branch protection、必過 CI、衝突、權限不足都視為「預期失敗」記錄、不拋例外。
    訊息一律經 redact 遮蔽 token。
    """
    import httpx

    base = f"https://api.github.com/repos/{config.PUBLISH_REPO}/pulls/{pr_number}"
    merge_url = f"{base}/merge"
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            last = ""
            for attempt in range(_MERGE_POLL_TRIES):
                r = await client.put(merge_url, json=payload, headers=_gh_headers())
                if r.status_code == 200:
                    data = r.json()
                    return True, "已自動合併", data.get("sha")
                # 405：尚不可合併（通常 mergeable 仍在計算中）→ 等待重試。
                if r.status_code == 405 and attempt < _MERGE_POLL_TRIES - 1:
                    last = redact(r.text[:200])
                    await asyncio.sleep(_MERGE_POLL_DELAY)
                    continue
                # 其餘狀態（409 衝突 / 403 權限 / 422 等）視為預期失敗，直接回報。
                return False, f"merge 失敗（{r.status_code}）：{redact(r.text[:200])}", None
            return False, f"merge 失敗（PR 尚不可合併）：{last}", None
    except Exception as e:  # 網路/API 例外都不可讓服務崩潰
        return False, "merge 失敗（請求例外）：" + redact(str(e)), None


async def publish(
    cwd,
    session_id: str,
    requirement: str,
    *,
    make_pr: bool = True,
    do_merge: bool = False,
    merge_method: str = "merge",
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

    ok, info, number = await _open_pr(pr_payload(requirement, branch, config.PUBLISH_BASE))
    if not ok:
        return PublishResult(True, "已 push，但 " + info, branch=branch, repo=repo, pushed=True)

    result = PublishResult(
        True,
        "已 push 並建立 PR",
        branch=branch,
        repo=repo,
        pushed=True,
        pr_url=info,
        pr_number=number,
    )

    if not do_merge:
        return result

    if number is None:
        result.detail += "；無法取得 PR number，略過自動合併"
        return result

    merged, mmsg, sha = await _merge_pr(number, merge_payload(requirement, merge_method))
    result.merged = merged
    result.merge_sha = sha
    if merged:
        result.detail = f"已 push、建立 PR 並自動合併進 {config.PUBLISH_BASE}"
    else:
        result.detail += "；" + mmsg
    return result
