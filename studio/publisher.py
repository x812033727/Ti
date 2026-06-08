"""把工作室成果（workspace 內的獨立 git repo）發佈到 GitHub。

對外動作，預設關閉：需設定 `GITHUB_TOKEN` 與 `TI_PUBLISH_REPO`（owner/repo）才會啟用。
流程：在 workspace repo 建立分支 → 加上帶 token 的 remote → push → 視設定開 PR → 視設定合併。
純邏輯（分支命名 / URL 組裝 / token 遮蔽 / PR payload / 狀態分類）與實際 IO 分離，方便單元測試。

合併不再「不等 CI 直接 PUT」：先等 CI（`_wait_for_ci`），再合併（`_merge_pr` + `_merge_flow`
重試）。四／六種結局（MERGED / CI_FAILED / BLOCKED / CONFLICT / TIMEOUT / ERROR）皆寫進
`PublishResult.outcome` 與 detail，全程不丟例外，杜絕 silent failed。
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from enum import Enum

from . import config, runner

_PR_NUM_RE = re.compile(r"/pull/(\d+)")


class MergeOutcome(str, Enum):
    """合併的終局類別（繼承 str 便於序列化進 to_dict / 事件）。"""

    MERGED = "merged"  # 成功合併
    CI_FAILED = "ci_failed"  # CI 未過（明確失敗）
    BLOCKED = "blocked"  # 被分支保護擋下（缺審核 / 不符規則）
    CONFLICT = "conflict"  # 合併衝突或分支落後（stale / dirty）
    TIMEOUT = "timeout"  # 等待 CI 逾時
    ERROR = "error"  # API rate limit / 5xx / 網路例外 / 未知狀態


# 給人看的結局標籤（寫進 detail，讓外層與使用者都能讀懂卡關原因）。
_OUTCOME_LABEL: dict[MergeOutcome, str] = {
    MergeOutcome.MERGED: "已合併",
    MergeOutcome.CI_FAILED: "CI 未過",
    MergeOutcome.BLOCKED: "被保護擋下（缺審核或不符分支保護規則）",
    MergeOutcome.CONFLICT: "合併衝突或分支落後（stale）",
    MergeOutcome.TIMEOUT: "等待 CI 逾時",
    MergeOutcome.ERROR: "API／網路錯誤",
}


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
    outcome: MergeOutcome | None = None  # 合併結局（未嘗試合併時為 None）

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
            # 以字串輸出（MergeOutcome 繼承 str），未嘗試合併為 None，不破壞既有鍵。
            "outcome": self.outcome.value if self.outcome else None,
        }


# --- 純邏輯（可單測，無 IO）-------------------------------------------


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


# `mergeable_state` 是 GitHub REST PR 物件上「未正式文件化」但長期穩定的欄位。
# 契約：`unknown` 代表 GitHub 仍在背景計算，並非終局——必須在 IO 層（_get_pr_status）
# re-poll 至收斂，不可當結局輸出。以下映射只在「已收斂」的狀態上做終局分類。
_MERGE_STATE_OUTCOME: dict[str, MergeOutcome] = {
    "clean": MergeOutcome.MERGED,  # 可合併
    "has_hooks": MergeOutcome.MERGED,  # 可合併（base 設了 pre-receive hook）
    "behind": MergeOutcome.CONFLICT,  # 落後 base（stale，需 update-branch）
    "dirty": MergeOutcome.CONFLICT,  # 真實合併衝突
    "blocked": MergeOutcome.BLOCKED,  # 必要檢查／審核未滿足
    "unstable": MergeOutcome.BLOCKED,  # 非必要檢查失敗／進行中
    "draft": MergeOutcome.BLOCKED,  # 草稿 PR 不可合併
    "unknown": MergeOutcome.ERROR,  # 收斂前不該走到這；走到視為錯誤
}


def classify_merge_state(pr: dict | None) -> MergeOutcome:
    """把 PR 物件的 `mergeable_state` 映射成結局類別。

    未知（非已知列舉）一律 fallback 到 ERROR，絕不默默當 clean / 可合併。
    """
    state = (pr or {}).get("mergeable_state") or "unknown"
    return _MERGE_STATE_OUTCOME.get(state, MergeOutcome.ERROR)


# 卡關原因類別（人類可讀標籤）。`mergeable_state == blocked` 在 GitHub 同時涵蓋
# 「required check 未過」與「缺審核／不符保護規則」，光看狀態無法區分——必須結合
# CI 摘要狀態（summarize_checks 的 state）才能細分，故本函式同時吃 PR 與 check_state。
_BLOCK_REASON_LABEL: dict[str, str] = {
    "ci_failed": "CI 未過",
    "needs_review": "缺審核或不符分支保護規則",
    "stale": "分支落後 base（stale，需更新分支）",
    "conflict": "合併衝突",
    "mergeable": "可合併（無卡關）",
    "unknown": "狀態未知（GitHub 計算中或未預期狀態）",
}


def classify_block_reason(pr: dict | None, check_state: str | None = None) -> tuple[str, str]:
    """把「為何無法合併」精準分類為四類之一，回傳 (category, 人類可讀說明)。

    category ∈ {"ci_failed", "needs_review", "stale", "conflict", "mergeable", "unknown"}：
    - dirty                 → conflict（真實合併衝突）
    - behind                → stale（落後 base，需 update-branch）
    - blocked/unstable/draft：
        - check_state == "fail" → ci_failed（必要檢查未過）
        - 否則（CI 已過／無 CI／進行中）→ needs_review（缺審核／不符保護規則）
    - clean/has_hooks       → mergeable
    - 其餘（unknown／未知值）→ unknown

    解決原始 405 HTTP text 含糊的問題：blocked 不再一律報「被保護擋下」，而是依
    CI 狀態區分「CI 未過」與「缺審核」。
    """
    state = (pr or {}).get("mergeable_state") or "unknown"
    if state == "dirty":
        category = "conflict"
    elif state == "behind":
        category = "stale"
    elif state in ("blocked", "unstable", "draft"):
        category = "ci_failed" if check_state == "fail" else "needs_review"
    elif state in ("clean", "has_hooks"):
        category = "mergeable"
    else:
        category = "unknown"
    return category, _BLOCK_REASON_LABEL[category]


# check-run 的 conclusion 視為失敗的集合（保守：不確定狀態不放行合併）。
_FAIL_CONCLUSIONS = {
    "failure",
    "timed_out",
    "cancelled",
    "action_required",
    "startup_failure",
    "stale",
}


def summarize_checks(check_runs: list | None, status: dict | None) -> tuple[str, str]:
    """把 check-runs 陣列與 legacy combined status 歸併成 (state, detail)。

    state ∈ {"pass", "fail", "pending"}。合併兩套來源，任一為 fail 即 fail。
    明確規則：check-runs 與 status 皆空（無任何 CI）→ 回 pass，detail 註記「無 CI」，
    避免無 CI 的倉庫空等到逾時。
    """
    runs = check_runs or []
    status = status or {}
    status_state = status.get("state")  # success / failure / pending / None
    status_total = int(status.get("total_count", 0) or 0)

    if not runs and status_total == 0:
        return "pass", "無 CI（無 check-runs 與 status）"

    # 1) 任一失敗即 fail（fail-fast，對齊 gh pr checks --fail-fast）。
    failed = [r for r in runs if (r.get("conclusion") in _FAIL_CONCLUSIONS)]
    if failed or status_state == "failure":
        names = [r.get("name", "?") for r in failed][:3]
        suffix = ("：" + ", ".join(names)) if names else ""
        return "fail", f"CI 失敗（{len(failed)} 個 check 失敗{('／legacy status failure' if status_state == 'failure' else '')}）{suffix}"

    # 2) 任一未完成（或 legacy status pending）即 pending。
    pending = [r for r in runs if r.get("status") != "completed"]
    if pending or status_state == "pending":
        names = [r.get("name", "?") for r in pending][:3]
        suffix = ("：" + ", ".join(names)) if names else ""
        return "pending", f"CI 進行中（{len(pending)} 個 check 未完成{suffix}）"

    return "pass", "CI 全數通過"


def _backoff(attempt: int, base: float) -> float:
    """指數 backoff，封頂 60 秒。attempt 從 0 起算。"""
    return min(base * (2**attempt), 60.0)


# --- IO 工具 -----------------------------------------------------------


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {config.GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
    }


def _api(path: str) -> str:
    return f"https://api.github.com/repos/{config.PUBLISH_REPO}{path}"


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

    headers = _headers()
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(_api("/pulls"), json=payload, headers=headers)
    if r.status_code in (200, 201):
        return True, r.json().get("html_url", "")
    return False, f"PR 建立失敗（{r.status_code}）：{r.text[:200]}"


async def _get_pr_status(
    number: int, *, sleep=asyncio.sleep, retries: int = 5, interval: float = 2.0
) -> dict | None:
    """查 PR 的結構化狀態（mergeable / mergeable_state / head sha）。

    `mergeable_state == unknown`（或 mergeable 為 None）代表 GitHub 仍在計算，視為「未收斂」，
    帶上限 re-poll；超過上限仍未收斂則回傳最後一次結果（caller 端 classify 會落到 ERROR）。
    任何 API/網路失敗回 None。
    """
    import httpx

    headers = _headers()
    data: dict | None = None
    for i in range(retries + 1):
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.get(_api(f"/pulls/{number}"), headers=headers)
        except Exception:
            return None
        if r.status_code != 200:
            return None
        data = r.json()
        ms = data.get("mergeable_state")
        if data.get("mergeable") is not None and ms not in (None, "unknown"):
            return data
        if i < retries:
            await sleep(interval)
    return data


async def _fetch_ci(head_sha: str) -> tuple[list, dict] | None:
    """抓 head sha 的 check-runs（翻頁）與 legacy combined status。失敗回 None。"""
    import httpx

    headers = _headers()
    runs: list = []
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            page = 1
            while page <= 20:  # 上限 2000 個 check，足夠且避免異常無限翻頁
                r = await client.get(
                    _api(f"/commits/{head_sha}/check-runs"),
                    params={"per_page": 100, "page": page},
                    headers=headers,
                )
                if r.status_code != 200:
                    return None
                body = r.json()
                batch = body.get("check_runs", []) or []
                runs.extend(batch)
                total = int(body.get("total_count", len(runs)) or len(runs))
                if not batch or len(runs) >= total:
                    break
                page += 1
            sr = await client.get(_api(f"/commits/{head_sha}/status"), headers=headers)
            status = sr.json() if sr.status_code == 200 else {}
    except Exception:
        return None
    return runs, status


async def _wait_for_ci(
    head_sha: str,
    *,
    timeout: float,
    interval: float,
    sleep=asyncio.sleep,
    max_fetch_errors: int = 3,
) -> tuple[str, str]:
    """輪詢 head sha 的 CI，直到 pass/fail 或逾時。回傳 (state, detail)。

    state ∈ {"pass", "fail", "timeout", "error"}：pending 續等、fail 早退、逾時早退。

    韌性：
    - 單次查詢失敗（API／網路抖動）不立即放棄——容忍連續 `max_fetch_errors` 次後才回 error，
      期間仍計入 timeout，故失敗也不會無限重試。
    - interval ≤ 0 時，pending 一輪即視為已達 timeout，避免 waited 永不增加的無限迴圈。
    """
    # 防 interval 非正導致 waited 永不增加：用一個正的步進來累計等待時間。
    step = interval if interval > 0 else (timeout + 1)
    waited = 0.0
    last_detail = "未知"
    fetch_errors = 0
    while True:
        fetched = await _fetch_ci(head_sha)
        if fetched is None:
            fetch_errors += 1
            last_detail = f"查詢 CI 狀態失敗（第 {fetch_errors} 次）"
            # 連續多次失敗、或已耗盡 timeout 才放棄，避免單次抖動誤判 ERROR、也不無限重試。
            if fetch_errors >= max_fetch_errors or waited >= timeout:
                return (
                    "error",
                    f"查詢 CI 狀態連續失敗（API／網路錯誤，已重試 {fetch_errors} 次）",
                )
            await sleep(interval)
            waited += step
            continue

        fetch_errors = 0  # 查詢成功就重置連續失敗計數
        state, last_detail = summarize_checks(*fetched)
        if state in ("pass", "fail"):
            return state, last_detail
        # pending
        if waited >= timeout:
            return "timeout", f"等待 CI 逾時（已等待 {int(waited)}s，最後狀態：{last_detail}）"
        await sleep(interval)
        waited += step


async def _update_branch(number: int) -> bool:
    """呼叫 PUT /pulls/{n}/update-branch 把 base 的最新 commit 併進 PR 分支（修 stale）。"""
    import httpx

    headers = _headers()
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.put(_api(f"/pulls/{number}/update-branch"), headers=headers)
        return r.status_code in (200, 202)
    except Exception:
        return False


async def _merge_pr(number: int, payload: dict) -> tuple[MergeOutcome, str, bool]:
    """單次合併嘗試：PUT merge，把結果分流為 (outcome, detail, retryable)。

    不丟例外。分流：
    - 200 → MERGED（不重試）
    - 409 → CONFLICT，retryable=True（base 已變動／落後，可 update-branch 後重試）
    - 405 / 422 → BLOCKED，retryable=False（受保護／不符規則，重試只是白等）
    - 5xx / 網路例外 → ERROR，retryable=True（暫時性，可退避重試）
    - 其他 → ERROR，retryable=False
    """
    import httpx

    headers = _headers()
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.put(_api(f"/pulls/{number}/merge"), json=payload, headers=headers)
    except Exception as e:  # 網路等例外也不外拋，轉成可讀錯誤
        return MergeOutcome.ERROR, f"merge 請求失敗：{type(e).__name__}", True
    if r.status_code == 200:
        return MergeOutcome.MERGED, r.json().get("sha", ""), False
    if r.status_code == 409:
        return MergeOutcome.CONFLICT, f"分支落後或 base 已變動（409）：{r.text[:200]}", True
    if r.status_code in (405, 422):
        return MergeOutcome.BLOCKED, f"不可合併／受保護（{r.status_code}）：{r.text[:200]}", False
    if r.status_code >= 500:
        return MergeOutcome.ERROR, f"GitHub 伺服器錯誤（{r.status_code}）：{r.text[:200]}", True
    return MergeOutcome.ERROR, f"merge 失敗（{r.status_code}）：{r.text[:200]}", False


async def _merge_flow(
    number: int,
    payload: dict,
    *,
    ci_timeout: float,
    ci_interval: float,
    retries: int,
    sleep=asyncio.sleep,
) -> tuple[MergeOutcome, str]:
    """合併協調器：每輪「查狀態 → 等 CI → 合併」，可重試錯誤則 update-branch 後重試。

    - 先 `_wait_for_ci`：CI fail → CI_FAILED 早退；逾時 → TIMEOUT 早退；查詢失敗 → ERROR。
    - 再 `_merge_pr`：成功 → MERGED；不可重試 → 用 `classify_merge_state` 精準回報 BLOCKED／CONFLICT。
    - 可重試（409／behind）→ `_update_branch` 修 stale 後退避重試；下一輪會重抓 head sha 並
      重新等該 sha 的 CI（update-branch 會產生新 commit）。超過次數才放棄。
    """
    last_outcome, last_detail = MergeOutcome.ERROR, "未知錯誤"
    for attempt in range(retries + 1):
        status = await _get_pr_status(number, sleep=sleep)
        if status is None:
            return MergeOutcome.ERROR, "無法取得 PR 狀態（API／網路錯誤）"

        head_sha = (status.get("head") or {}).get("sha", "")
        ci_state, ci_detail = await _wait_for_ci(
            head_sha, timeout=ci_timeout, interval=ci_interval, sleep=sleep
        )
        if ci_state == "fail":
            return MergeOutcome.CI_FAILED, ci_detail
        if ci_state == "timeout":
            return MergeOutcome.TIMEOUT, ci_detail
        if ci_state == "error":
            return MergeOutcome.ERROR, ci_detail

        # CI pass / 無 CI → 嘗試合併
        outcome, detail, retryable = await _merge_pr(number, payload)
        if outcome == MergeOutcome.MERGED:
            return outcome, detail
        last_outcome, last_detail = outcome, detail

        exhausted = attempt >= retries
        if not retryable or exhausted:
            # 用結構化狀態精準分類卡關原因（CI 已過卻 BLOCKED → 多半是缺審核／保護規則）。
            refined = classify_merge_state(status)
            if refined in (MergeOutcome.BLOCKED, MergeOutcome.CONFLICT):
                # 結合 CI 摘要狀態細分「CI 未過／缺審核／stale／衝突」，取代含糊的 HTTP text。
                _, reason = classify_block_reason(status, ci_state)
                detail = f"{reason}（mergeable_state={status.get('mergeable_state')}；{detail}）"
                outcome = refined
            if retryable and exhausted:
                detail = f"{detail}（已達重試上限 {retries} 次）"
            return outcome, detail

        # 可重試：先修 stale（update-branch），退避後重試（下一輪重抓 sha 並重等 CI）。
        await _update_branch(number)
        await sleep(_backoff(attempt, ci_interval))

    return last_outcome, last_detail


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

    # 自動合併（TI_PUBLISH_MERGE 開啟時）：先等 CI 再合併。任何失敗皆不丟例外，皆有 outcome+detail。
    if res.pr_number is None:
        res.outcome = MergeOutcome.ERROR
        res.detail = "已 push 並建立 PR，但無法解析 PR 編號，未自動合併"
        return res

    outcome, minfo = await _merge_flow(
        res.pr_number,
        merge_payload(branch),
        ci_timeout=config.PUBLISH_CI_TIMEOUT,
        ci_interval=config.PUBLISH_CI_INTERVAL,
        retries=config.PUBLISH_MERGE_RETRIES,
    )
    res.outcome = outcome
    if outcome == MergeOutcome.MERGED:
        res.merged = True
        res.detail = "已 push、建立 PR 並合併"
    else:
        label = _OUTCOME_LABEL.get(outcome, "未合併")
        res.detail = f"已 push 並建立 PR，但未合併（{label}）：" + redact(minfo)
    return res
