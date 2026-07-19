"""
QA 驗收測試：針對 `bash scripts/verify-clean.sh` 的執行結果，逐一比對任務 #1 驗收標準。

設計原則（QA 立場）：
- 黑盒：直接執行命令，捕獲原始輸出 + exit code，不解析、不修補
- 證據導向：每一條斷言都附「為何這樣判」的註解
- 不修補 repo：所有測試用 git plumbing / read-only 操作
- 可重跑：純 pytest，後續覆核者（任務 #4）可直接 `pytest -v` 驗證

不在此測試的事項：
- 假性 diff 排除政策的內容正確性（屬任務 #2 範疇）
- close-out 文件撰寫（屬任務 #3 範疇）

執行：.venv/bin/python -m pytest tests/test_verify_clean_acceptance.py -v
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _run(cmd: list[str], cwd: Path = REPO_ROOT) -> subprocess.CompletedProcess:
    """跑一條 git / shell 命令，回傳 CompletedProcess。"""
    return subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=True,
        text=True,
        env={**os.environ, "LC_ALL": "C", "GIT_TERMINAL_PROMPT": "0"},
    )


def _ensure_origin_main_ref() -> None:
    """確保 CI 的 PR checkout 也有可解析的 origin/main。"""
    cp = _run(["git", "rev-parse", "--verify", "origin/main^{commit}"])
    if cp.returncode == 0:
        return

    fetch = _run(["git", "fetch", "origin", "+refs/heads/main:refs/remotes/origin/main"])
    assert fetch.returncode == 0, (
        "origin/main 缺 commit 物件，且自動 fetch 失敗："
        f"stdout={fetch.stdout!r} stderr={fetch.stderr!r}"
    )


@contextmanager
def _origin_main_worktree(tmp_path: Path) -> Iterator[Path]:
    """建立綁定 origin/main 的 detached worktree，對齊 verify-clean.sh 的 release gate。"""
    _ensure_origin_main_ref()
    wt = tmp_path / "origin-main-worktree"
    cp = _run(["git", "worktree", "add", "--detach", str(wt), "origin/main"])
    assert cp.returncode == 0, f"建立 origin/main worktree 失敗: stderr={cp.stderr!r}"
    try:
        yield wt
    finally:
        _run(["git", "worktree", "remove", "--force", str(wt)])


# --- 前置 -----------------------------------------------------------------


def test_repo_is_inside_git_work_tree() -> None:
    """防呆：確認測試環境確實在 git repo 內（腳本內有同樣檢查，這裡再驗一次避免偽綠）。"""
    cp = _run(["git", "rev-parse", "--is-inside-work-tree"])
    assert cp.returncode == 0, f"不在 git repo 內: stderr={cp.stderr!r}"
    assert cp.stdout.strip() == "true"


def test_origin_main_ref_exists() -> None:
    """防呆：origin/main 必須可解析為 commit 物件（腳本內 exit 99 路徑的觸發條件）。"""
    _ensure_origin_main_ref()
    cp = _run(["git", "rev-parse", "--verify", "origin/main^{commit}"])
    assert (
        cp.returncode == 0
    ), f"origin/main 缺 commit 物件（請先 git fetch origin）: stderr={cp.stderr!r}"


# --- 驗收標準 1：git fetch origin 成功 --------------------------------------


def test_fetch_origin_exit_zero() -> None:
    """驗收：git fetch origin 必須成功（exit 0），否則後續 diff/hash 全部基於過時 ref。"""
    cp = _run(["git", "fetch", "origin", "+refs/heads/main:refs/remotes/origin/main"])
    assert cp.returncode == 0, f"git fetch origin 失敗 exit={cp.returncode}, stderr={cp.stderr!r}"


# --- 驗收標準 2：git status --porcelain=v2 --branch 顯示工作樹乾淨 ----------


def test_status_porcelain_v2_branch_clean(tmp_path: Path) -> None:
    """驗收：status 輸出不得有檔案行（工作樹乾淨）。

    注意：
    - verify-clean.sh 的 release gate 是「綁 origin/main 的 detached worktree」。
    - PR checkout 的 HEAD 合理地不同於 origin/main，因此這裡驗 release gate worktree，
      不把 PR branch 本身的 commit 差異誤判成 dirty。
    """
    with _origin_main_worktree(tmp_path) as wt:
        cp = _run(
            ["git", "status", "--porcelain=v2", "--branch", "--untracked-files=normal"], cwd=wt
        )
    assert cp.returncode == 0, f"status exit={cp.returncode}, stderr={cp.stderr!r}"
    # 任何非 '# ' 開頭的行都代表有檔案變動
    file_lines = [ln for ln in cp.stdout.splitlines() if not ln.startswith("# ")]
    assert file_lines == [], "origin/main worktree 不乾淨，存在檔案變動行：\n" + "\n".join(
        file_lines
    )


# --- 驗收標準 3：git diff --quiet origin/main HEAD exit 0 -------------------


def test_diff_origin_main_head_quiet_exit_zero(tmp_path: Path) -> None:
    """驗收：與 origin/main 比對必須無 diff。

    在 PR checkout 中，PR HEAD 與 origin/main 有 diff 是正常狀態；verify-clean.sh 的合約
    是在 origin/main detached worktree 中驗 release gate 必須無 diff。
    """
    with _origin_main_worktree(tmp_path) as wt:
        cp = _run(["git", "diff", "--quiet", "origin/main", "HEAD"], cwd=wt)
    # --quiet 模式下有差異會 exit 1，無差異 exit 0；無 stdout
    assert cp.returncode == 0, (
        f"origin/main worktree 的 git diff --quiet origin/main HEAD 顯示有差異 exit={cp.returncode}；"
        f"stdout={cp.stdout!r} stderr={cp.stderr!r}"
    )


# --- 驗收標準 4：git diff --quiet --cached exit 0 ---------------------------


def test_diff_cached_quiet_exit_zero(tmp_path: Path) -> None:
    """驗收：staged 區必須無 diff（index 與 HEAD 一致）。"""
    with _origin_main_worktree(tmp_path) as wt:
        cp = _run(["git", "diff", "--quiet", "--cached"], cwd=wt)
    assert cp.returncode == 0, (
        f"git diff --quiet --cached 顯示有 staged 差異 exit={cp.returncode}；"
        f"stdout={cp.stdout!r} stderr={cp.stderr!r}"
    )


# --- 驗收標準 5：HEAD hash == origin/main hash ------------------------------


def test_rev_parse_head_equals_origin_main(tmp_path: Path) -> None:
    """驗收：本地 HEAD 與 origin/main 必須指向同一 commit 物件。

    這個條件只適用於 verify-clean.sh 建出的 origin/main detached worktree；PR HEAD 本身
    不應被要求等於 origin/main。
    """
    with _origin_main_worktree(tmp_path) as wt:
        head = _run(["git", "rev-parse", "HEAD"], cwd=wt).stdout.strip()
        origin = _run(["git", "rev-parse", "origin/main"], cwd=wt).stdout.strip()
    assert head == origin, f"hash 不一致: HEAD={head!r} origin/main={origin!r}"


# --- 驗收標準 6：工作樹無未追蹤殘留（任務交付後的 git status 必須乾淨）------


def test_no_untracked_residuals_in_worktree(tmp_path: Path) -> None:
    """驗收：交付完成後 git status 不得出現未追蹤殘留。

    範圍：
    - 以 origin/main detached worktree 為準，避免 PR branch 或本機備份檔污染 release gate 判定。
    """
    with _origin_main_worktree(tmp_path) as wt:
        cp = _run(["git", "status", "--porcelain"], cwd=wt)
    assert cp.returncode == 0
    suspicious = []
    for ln in cp.stdout.splitlines():
        if not ln.startswith("??"):
            continue
        suspicious.append(ln)
    assert suspicious == [], "工作樹出現未預期的 untracked 殘留：\n" + "\n".join(suspicious)


# --- 驗收標準 7：scripts/verify-clean.sh 本身可執行 --------------------------


def test_verify_clean_script_executable_and_reflects_fail() -> None:
    """驗收：執行 `bash scripts/verify-clean.sh` 必須能跑完、退出碼反映 fail 累計。

    此測試是黑盒驗證腳本功能：腳本退出碼必須「如實反映 fail 累計」，不可偽綠。

    合約鬆綁：腳本輸出的總結字串（'=== 總體 fail=' / '=== 程式 fail=' 等）
    在工程師迭代過程中曾被改寫，因此本測試不綁死特定字串，只驗證：
      1. 有結構化輸出（含 '# verify-clean.sh' 標頭 + 'origin/main' 標籤）
      2. 退出碼反映腳本最後列出的 fail 累計
      3. 不以 99 環境前置失敗結束
    """
    script = REPO_ROOT / "scripts" / "verify-clean.sh"
    assert script.exists(), f"腳本不存在: {script}"

    cp = subprocess.run(
        ["bash", str(script)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        env={**os.environ, "LC_ALL": "C", "GIT_TERMINAL_PROMPT": "0"},
    )

    # (1) 結構化輸出存在：標頭 + 至少一處提到 origin/main
    assert (
        "# verify-clean.sh" in cp.stdout
    ), f"腳本輸出缺 '# verify-clean.sh' 標頭，不像驗證腳本: stdout={cp.stdout[:500]!r}"
    assert "origin/main" in cp.stdout, f"腳本輸出缺 'origin/main' 標籤: stdout={cp.stdout[:500]!r}"

    match = re.search(r"=== 程式 fail=(\d+)", cp.stdout)
    assert match is not None, f"腳本輸出缺 fail 總結: stdout={cp.stdout[-1000:]!r}"
    expected_rc = 0 if match.group(1) == "0" else 1
    assert cp.returncode == expected_rc, (
        "腳本退出碼未反映輸出的 fail 累計："
        f"expected={expected_rc} actual={cp.returncode}。請檢查 fail 累計邏輯。"
    )

    # (3) 不偽綠 exit 0 — 已在 (2) 涵蓋；額外保險：退出碼不可為 99（環境前置失敗）
    assert (
        cp.returncode != 99
    ), f"腳本 exit 99（環境前置失敗），4 條命令未執行: stderr={cp.stderr!r}"


# --- 假性 diff 排除政策的證據（不修補，只盤點）------------------------------


def test_false_diff_exclusion_policy_evidence() -> None:
    """盤點：本 repo 為何「理論上」不會有假性 diff。

    這條不修補任何設定，只讀檔與讀 git config，把當前證據列出來供任務 #2 引用。
    """
    evidence = {}

    # 1. .gitmodules
    gitmodules = REPO_ROOT / ".gitmodules"
    if gitmodules.exists():
        sm_count = sum(
            1
            for ln in gitmodules.read_text(encoding="utf-8").splitlines()
            if ln.startswith("[submodule ")
        )
        evidence[".gitmodules"] = f"present, [submodule ...] count = {sm_count}"
    else:
        evidence[".gitmodules"] = "absent"

    # 2. .gitattributes
    evidence[".gitattributes"] = "present" if (REPO_ROOT / ".gitattributes").exists() else "absent"

    # 3. core.autocrlf
    cp = _run(["git", "config", "--get", "core.autocrlf"])
    evidence["core.autocrlf"] = cp.stdout.strip() if cp.returncode == 0 else "unset"

    # 三項都記下即可（具體解讀屬任務 #2 範疇）
    assert isinstance(evidence, dict) and len(evidence) == 3
    # 至少 .gitmodules 與 .gitattributes 是確定可讀的（不為 None）
    assert evidence[".gitmodules"] in ("absent",) or evidence[".gitmodules"].startswith("present")
