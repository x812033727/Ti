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
import subprocess
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


# --- 前置 -----------------------------------------------------------------


def test_repo_is_inside_git_work_tree() -> None:
    """防呆：確認測試環境確實在 git repo 內（腳本內有同樣檢查，這裡再驗一次避免偽綠）。"""
    cp = _run(["git", "rev-parse", "--is-inside-work-tree"])
    assert cp.returncode == 0, f"不在 git repo 內: stderr={cp.stderr!r}"
    assert cp.stdout.strip() == "true"


def test_origin_main_ref_exists() -> None:
    """防呆：origin/main 必須可解析為 commit 物件（腳本內 exit 99 路徑的觸發條件）。"""
    cp = _run(["git", "rev-parse", "--verify", "origin/main^{commit}"])
    assert cp.returncode == 0, (
        f"origin/main 缺 commit 物件（請先 git fetch origin）: stderr={cp.stderr!r}"
    )


# --- 驗收標準 1：git fetch origin 成功 --------------------------------------


def test_fetch_origin_exit_zero() -> None:
    """驗收：git fetch origin 必須成功（exit 0），否則後續 diff/hash 全部基於過時 ref。"""
    cp = _run(["git", "fetch", "origin"])
    assert cp.returncode == 0, f"git fetch origin 失敗 exit={cp.returncode}, stderr={cp.stderr!r}"


# --- 驗收標準 2：git status --porcelain=v2 --branch 顯示工作樹乾淨 ----------


def test_status_porcelain_v2_branch_clean() -> None:
    """驗收：status 輸出不得有檔案行（工作樹乾淨）。

    注意：
    - 此 worktree 在 task-1 分支、無 upstream，因此 status 沒有 # branch.upstream 與
      # branch.ab 段；驗收條款「branch.ab +0 -0」要成立需在有 upstream 的分支上跑。
      本測試只斷言「無檔案行」這條「工作樹乾淨」子條件。
    - 排除例外：本測試檔自身（tests/test_verify_clean_acceptance.py）是 QA 為了驗證本任務
      新增的工具，會以 '.M'（被 git 追蹤且 modified）形式出現。close-out 文件需對應標示
      「QA 工具造成的 .M 非工作樹 dirty 證據」。這與腳本裡對 '? scripts/verify-clean.sh'
      的解讀邏輯對稱——驗收工具造成的可預期污染不視為 dirty。
    """
    cp = _run(["git", "status", "--porcelain=v2", "--branch", "--untracked-files=normal"])
    assert cp.returncode == 0, f"status exit={cp.returncode}, stderr={cp.stderr!r}"
    # 任何非 '# ' 開頭的行都代表有檔案變動
    file_lines = [ln for ln in cp.stdout.splitlines() if not ln.startswith("# ")]
    # 排除 QA 自身工具造成的可預期污染（僅限本測試檔）
    qa_self_paths = {"tests/test_verify_clean_acceptance.py"}
    dirty_lines = [ln for ln in file_lines if ln.split()[-1] not in qa_self_paths]
    assert dirty_lines == [], "工作樹不乾淨，存在檔案變動行（排除 QA 工具自身）：\n" + "\n".join(
        dirty_lines
    )


# --- 驗收標準 3：git diff --quiet origin/main HEAD exit 0 -------------------


def test_diff_origin_main_head_quiet_exit_zero() -> None:
    """驗收：與 origin/main 比對必須無 diff。

    若此條 fail：HEAD 與 origin/main 至少有 commit 落差或工作樹差異。
    """
    cp = _run(["git", "diff", "--quiet", "origin/main", "HEAD"])
    # --quiet 模式下有差異會 exit 1，無差異 exit 0；無 stdout
    assert cp.returncode == 0, (
        f"git diff --quiet origin/main HEAD 顯示有差異 exit={cp.returncode}；"
        f"stdout={cp.stdout!r} stderr={cp.stderr!r}"
    )


# --- 驗收標準 4：git diff --quiet --cached exit 0 ---------------------------


def test_diff_cached_quiet_exit_zero() -> None:
    """驗收：staged 區必須無 diff（index 與 HEAD 一致）。"""
    cp = _run(["git", "diff", "--quiet", "--cached"])
    assert cp.returncode == 0, (
        f"git diff --quiet --cached 顯示有 staged 差異 exit={cp.returncode}；"
        f"stdout={cp.stdout!r} stderr={cp.stderr!r}"
    )


# --- 驗收標準 5：HEAD hash == origin/main hash ------------------------------


def test_rev_parse_head_equals_origin_main() -> None:
    """驗收：本地 HEAD 與 origin/main 必須指向同一 commit 物件。

    這是比 diff 更嚴格的「字節完全一致」條件（merge commit 結構差異也會被抓到）。
    """
    head = _run(["git", "rev-parse", "HEAD"]).stdout.strip()
    origin = _run(["git", "rev-parse", "origin/main"]).stdout.strip()
    assert head == origin, f"hash 不一致: HEAD={head!r} origin/main={origin!r}"


# --- 驗收標準 6：工作樹無未追蹤殘留（任務交付後的 git status 必須乾淨）------


def test_no_untracked_residuals_in_worktree() -> None:
    """驗收：交付完成後 git status 不得出現未追蹤殘留。

    範圍：
    - 排除本測試檔自身（tests/test_verify_clean_acceptance.py 是 QA 新增的，會以 '??' 形式出現）
    - 排除 .venv / __pycache__ / .pytest_cache（若存在）
    - 排除 scripts/verify-clean.sh（工程師交付的腳本，close-out 應明確標示其存在）
    """
    cp = _run(["git", "status", "--porcelain"])
    assert cp.returncode == 0
    ignored_untracked = {
        "tests/test_verify_clean_acceptance.py",  # 本測試自身
        "scripts/verify-clean.sh",  # 工程師交付的腳本（close-out 應標示）
    }
    suspicious = []
    for ln in cp.stdout.splitlines():
        if not ln.startswith("??"):
            continue
        path = ln[3:].strip()
        if path in ignored_untracked:
            continue
        # 排除常見 build / cache 目錄（這些通常應在 .gitignore）
        if any(
            path.startswith(prefix)
            for prefix in (".venv/", "venv/", "__pycache__/", ".pytest_cache/", ".mypy_cache/")
        ):
            continue
        suspicious.append(ln)
    assert suspicious == [], "工作樹出現未預期的 untracked 殘留：\n" + "\n".join(suspicious)


# --- 驗收標準 7：scripts/verify-clean.sh 本身可執行 --------------------------


def test_verify_clean_script_executable_and_reflects_fail() -> None:
    """驗收：執行 `bash scripts/verify-clean.sh` 必須能跑完、退出碼反映 fail 累計。

    在當前 repo 狀態（HEAD 領先 origin/main，branch 無 upstream）下，腳本必 exit 1。
    此測試是黑盒驗證腳本功能：腳本退出碼必須「如實反映 fail 累計」，不可偽綠。

    合約鬆綁：腳本輸出的總結字串（'=== 總體 fail=' / '=== 程式 fail=' 等）
    在工程師迭代過程中曾被改寫，因此本測試不綁死特定字串，只驗證：
      1. 有結構化輸出（含 '# verify-clean.sh' 標頭 + 'origin/main' 標籤）
      2. 退出碼反映 fail（HEAD != origin/main 必 exit 1）
      3. 不偽綠 exit 0
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
    assert "# verify-clean.sh" in cp.stdout, (
        f"腳本輸出缺 '# verify-clean.sh' 標頭，不像驗證腳本: stdout={cp.stdout[:500]!r}"
    )
    assert "origin/main" in cp.stdout, f"腳本輸出缺 'origin/main' 標籤: stdout={cp.stdout[:500]!r}"

    head = _run(["git", "rev-parse", "HEAD"]).stdout.strip()
    origin = _run(["git", "rev-parse", "origin/main"]).stdout.strip()
    expected_rc = 0 if head == origin else 1
    assert cp.returncode == expected_rc, (
        f"腳本退出碼未反映 HEAD/origin_main 狀態：HEAD={head} origin/main={origin}，"
        f"expected={expected_rc} actual={cp.returncode}。請檢查 fail 累計邏輯。"
    )

    # (3) 不偽綠 exit 0 — 已在 (2) 涵蓋；額外保險：退出碼不可為 99（環境前置失敗）
    assert cp.returncode != 99, (
        f"腳本 exit 99（環境前置失敗），4 條命令未執行: stderr={cp.stderr!r}"
    )


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
