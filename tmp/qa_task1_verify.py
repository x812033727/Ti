"""QA 任務#1 驗證腳本：基線重現 + 因果邊界 + 三項驗收。

設計目的：
  本腳本不修改任何被驗物（studio/、tests/），僅做唯讀驗證與證據收集。
  任務#1 核心問題：「未 re-export」是否為 7 模組收集失敗的完整單一因果？
  本腳本不把 grep 數量當因果證明；它驗證現況已綠，並用歷史對照確認
  可證明的是「無 redundant alias 的既有 re-export 觸發 ruff F401」。

驗證矩陣：
  A. 修法在位：studio/__init__.py 內含 redundant-alias re-export
  B. 業務邏輯未動：secure_write.py 無 git 差異（以最後 commit 為錨）
  C. 三項驗收命令：import / pytest collect / ruff
  D. 產品程式碼零改動：studio/、tests/、主要設定無差異
  E. 因果邊界：契約點只界定影響面；歷史對照證明 alias 修的是 F401

退出碼：0 = PASS、1 = FAIL
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
INIT = REPO / "studio" / "__init__.py"
SECURE = REPO / "studio" / "secure_write.py"


def _run(cmd: str, timeout: int = 60) -> tuple[int, str, str]:
    p = subprocess.run(
        cmd,
        shell=True,
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return p.returncode, p.stdout, p.stderr


def _ruff_f401_stdin(source: str) -> tuple[int, str, str]:
    p = subprocess.run(
        [
            "timeout",
            "30",
            "python3",
            "-m",
            "ruff",
            "check",
            "--isolated",
            "--select",
            "F401",
            "--stdin-filename",
            "studio/__init__.py",
            "-",
        ],
        cwd=REPO,
        input=source,
        capture_output=True,
        text=True,
        timeout=45,
    )
    return p.returncode, p.stdout, p.stderr


def check_init_has_reexport() -> tuple[bool, str]:
    text = INIT.read_text(encoding="utf-8")
    # 允許行內 # 註解
    has_alias = bool(
        re.search(r"^\s*from\s+\.\s+import\s+secure_write\s+as\s+secure_write\b",
                  text, re.M)
    )
    return has_alias, (
        f"studio/__init__.py 含 `from . import secure_write as secure_write`: {has_alias}"
    )


def check_secure_write_untouched() -> tuple[bool, str]:
    """用 git diff 證明 secure_write.py 本輪零改動。"""
    rc, out, _ = _run("git status --porcelain -- studio/secure_write.py")
    untouched = rc == 0 and out.strip() == ""
    sha = ""
    rs, sout, _ = _run("git log -1 --format=%H -- studio/secure_write.py")
    if rs == 0:
        sha = sout.strip()
    return untouched, (
        f"studio/secure_write.py 本輪零改動={untouched}（HEAD={sha[:12] if sha else '?'}）"
    )


def check_runtime_import() -> tuple[bool, str]:
    rc, out, err = _run(
        'timeout 30 python3 -c "from studio import secure_write; '
        'print(\'IMPORT_OK type=\', type(secure_write).__name__)"',
    )
    ok = rc == 0 and "IMPORT_OK" in out
    return ok, f"`from studio import secure_write` exit={rc} out={out.strip()!r} err={err.strip()!r}"


def check_pytest_collect() -> tuple[bool, str]:
    rc, out, err = _run(
        "timeout 60 python3 -m pytest --collect-only -q tests/autopilot", timeout=90
    )
    text = out + err
    collected_match = re.search(r"(\d+)\s+tests?\s+collected", text)
    # pytest 不報 errors 即代表 0；抓不到 errors 行視為 0
    errors_match = re.search(r"(\d+)\s+errors?", text)
    collected = int(collected_match.group(1)) if collected_match else -1
    errors = int(errors_match.group(1)) if errors_match else 0
    has_error_token = "error" in text.lower() and "0 errors" not in text.lower()
    # 防呆：若 pytest 確實有錯誤行但我們 regex 沒抓到，必須標記
    suspicious = (
        "Error" in text or "ERROR" in text or "Traceback" in text
    ) and collected != 660
    ok = rc == 0 and collected == 660 and errors == 0 and not suspicious
    return ok, (
        f"pytest --collect-only tests/autopilot: exit={rc} "
        f"collected={collected} errors={errors} (預期 660/0, suspicious={suspicious})"
    )


def check_ruff() -> tuple[bool, str]:
    rc, out, err = _run("timeout 30 python3 -m ruff check studio/")
    ok = rc == 0
    return ok, f"`ruff check studio/` exit={rc} out={out.strip()!r} err={err.strip()!r}"


def check_git_clean() -> tuple[bool, str]:
    """產品程式碼與主要設定零改動。

    ADR/DECISIONS 與 tmp/QA 腳本是本輪修正對象，不納入產品程式碼零改動判定。
    """
    rc, out, _ = _run(
        "git status --porcelain -- studio/ tests/ pyproject.toml "
        "ARCHITECTURE.md README.md 2>&1"
    )
    tracked_changes = [
        ln for ln in out.splitlines()
        if ln.strip() and not ln.startswith("??")
    ]
    # untracked 只在被驗物範圍內才算違規
    in_scope_untracked = [
        ln for ln in out.splitlines()
        if ln.startswith("??") and (
            "/studio/" in ln or "/tests/" in ln
            or ln.endswith("/studio") or ln.endswith("/tests")
        )
    ]
    ok = rc == 0 and not tracked_changes and not in_scope_untracked
    return ok, (
        f"產品程式碼與主要設定 git status 為空={ok}；"
        f"tracked_changes={len(tracked_changes)} in_scope_untracked={len(in_scope_untracked)} "
        f"輸出={out.strip() or '無'}"
    )


def check_causal_boundary() -> tuple[bool, str]:
    """因果邊界與歷史對照：
    1. 列舉 tests/autopilot 的「from studio import X」契約點，只作影響面資訊。
    2. 對照 f7be01e^：當時已有 `from . import secure_write`，不是未 re-export。
    3. 對照 ruff：無 alias 寫法報 F401，redundant alias 寫法通過。
    """
    rs, out, _ = _run(
        "grep -rEn '^\\s*from\\s+studio\\s+import\\b' tests/autopilot "
        r"| sed -E 's/^([^:]+):(\\s*)from studio import /\\1:\\2/'",
    )
    contract_lines = [ln for ln in out.splitlines() if ln.strip()]
    n = len(contract_lines)

    old_rc, old_src, old_err = _run("git show f7be01e^:studio/__init__.py")
    cur_rc, cur_src, cur_err = _run("git show f7be01e:studio/__init__.py")
    old_had_plain_reexport = "from . import secure_write" in old_src
    old_had_alias = "from . import secure_write as secure_write" in old_src
    cur_had_alias = "from . import secure_write as secure_write" in cur_src

    old_ruff_rc, old_ruff_out, old_ruff_err = _ruff_f401_stdin(old_src)
    cur_ruff_rc, cur_ruff_out, cur_ruff_err = _ruff_f401_stdin(cur_src)
    old_ruff_text = old_ruff_out + old_ruff_err
    cur_ruff_text = cur_ruff_out + cur_ruff_err
    old_reports_f401 = old_ruff_rc != 0 and "F401" in old_ruff_text
    current_alias_passes_ruff = cur_ruff_rc == 0

    ok = (
        rs == 0
        and n >= 1
        and old_rc == 0
        and cur_rc == 0
        and old_had_plain_reexport
        and not old_had_alias
        and cur_had_alias
        and old_reports_f401
        and current_alias_passes_ruff
    )

    if old_rc != 0 or cur_rc != 0:
        history_detail = f"history_error old={old_err.strip()!r} current={cur_err.strip()!r}"
    else:
        history_detail = (
            "歷史對照：f7be01e^ 已有 plain re-export="
            f"{old_had_plain_reexport}，舊版 alias={old_had_alias}；"
            f"f7be01e alias={cur_had_alias}"
        )
    return ok, (
        "結論：未 re-export 是 7 模組 collect 失敗完整單一因果 = 未證明；"
        "已證明的是 plain re-export 會觸發 ruff F401，redundant alias 會通過。\n"
        f"{history_detail}\n"
        f"ruff 對照：plain rc={old_ruff_rc} has_F401={old_reports_f401}；"
        f"alias rc={cur_ruff_rc} passes={current_alias_passes_ruff}\n"
        f"tests/autopilot 中 `from studio import ...` 契約點共 {n} 處（僅界定影響面）：\n"
        + "\n".join(f"  - {ln}" for ln in contract_lines[:10])
        + ("\n  ..." if n > 10 else "")
    )


def main() -> int:
    checks = [
        ("A.修法在位（redundant alias）", check_init_has_reexport),
        ("B.secure_write.py 業務邏輯未動", check_secure_write_untouched),
        ("C1.import 驗收", check_runtime_import),
        ("C2.pytest collect 660/0", check_pytest_collect),
        ("C3.ruff check studio/", check_ruff),
        ("D.產品程式碼零改動（git clean）", check_git_clean),
        ("E.因果邊界與歷史對照", check_causal_boundary),
    ]
    fails: list[str] = []
    for name, fn in checks:
        ok, detail = fn()
        marker = "PASS" if ok else "FAIL"
        print(f"[{marker}] {name}\n       {detail}")
        if not ok:
            fails.append(name)
    print()
    if fails:
        print(f"失敗項：{fails}")
        return 1
    print("全部 7 項檢查通過。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
