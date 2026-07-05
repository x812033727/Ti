"""QA 驗收：任務 #1 「實作需求」的交付真相測試。

上一輪問題是 blocker 文件宣稱 doc-only/零 .py，但 git 事實已有功能實作。
本測試改為驗證交付文件、git diff 與護欄測試三者一致。

執行：pytest tests/qa_task1_blocker_truthfulness.py -v
"""

from __future__ import annotations

import pathlib
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[1]
EXPECTED_TRACKED_PY = {
    "studio/autopilot.py",
    "studio/config.py",
    "tests/autopilot/test_daily_token_budget.py",
    "tests/test_task1_retry_doc.py",
}
OPTIONAL_VALIDATION_PY = {"tests/qa_task1_blocker_truthfulness.py"}


def sh(cmd: list[str]) -> str:
    return subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, check=True).stdout


def assert_py_scope_matches_delivery(changed: set[str]) -> None:
    missing = sorted(EXPECTED_TRACKED_PY - changed)
    unexpected = sorted(changed - EXPECTED_TRACKED_PY - OPTIONAL_VALIDATION_PY)
    assert not missing, f"交付文件列出的 .py 變更未出現在 diff：{missing}"
    assert not unexpected, f"出現未列入交付範圍的 .py 變更：{unexpected}"


def test_blocker_removed_and_delivery_doc_exists():
    assert not (ROOT / "BLOCKER_TASK1.md").exists(), "過時 blocker 文件應撤掉"
    doc_path = ROOT / "TASK1_DELIVERY.md"
    assert doc_path.exists(), "應改交正向交付文件 TASK1_DELIVERY.md"
    doc = doc_path.read_text(encoding="utf-8")
    assert "不是 doc-only" in doc
    assert "零 `.py` 變更" in doc
    assert "TI_AUTOPILOT_DAILY_TOKEN_BUDGET" in doc
    assert "沒東西可驗" not in doc


def test_actual_py_diff_matches_delivery_scope_against_origin_main():
    out = sh(["git", "diff", "--name-only", "origin/main", "--", "*.py"])
    changed = {p for p in out.splitlines() if p.strip()}
    assert_py_scope_matches_delivery(changed)


def test_actual_py_diff_against_merge_base():
    base = sh(["git", "merge-base", "HEAD", "origin/main"]).strip()
    assert base, "取不到 merge-base，無法驗證"
    out = sh(["git", "diff", "--name-only", base, "--", "*.py"])
    changed = {p for p in out.splitlines() if p.strip()}
    assert_py_scope_matches_delivery(changed)


def test_commit_message_matches_blocker_narrative():
    msg = sh(["git", "log", "-1", "--pretty=%s"]).strip()
    has_py_diff = bool(sh(["git", "diff", "--name-only", "HEAD~1..HEAD", "--", "*.py"]).strip())
    if "實作需求" in msg or "implement" in msg.lower():
        assert has_py_diff, (
            f"commit message 自稱『實作需求』（{msg!r}），卻沒改任何 .py——"
            "敘事與程式碼不一致，視為假綠燈"
        )


def test_task1_retry_doc_guardrail_passes():
    result = subprocess.run(
        [
            "python3",
            "-m",
            "pytest",
            "tests/test_task1_retry_doc.py::test_no_py_changed",
            "-v",
            "--tb=short",
            "-p",
            "no:cacheprovider",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, (
        f"任務#1 護欄測試應能驗證真實範圍：\n{result.stdout}\n{result.stderr}"
    )


def test_if_py_was_touched_then_test_covers_it():
    out = sh(["git", "diff", "--name-only", "origin/main", "--", "*.py"])
    changed = [p for p in out.splitlines() if p.strip()]
    if not changed:
        return  # AC1 已攔，這裡 no-op
    # 對每個被改的生產檔，必須能在 tests/ 找到引用它的測試檔
    test_root = ROOT / "tests"
    test_index: set[str] = set()
    for t in test_root.rglob("test_*.py"):
        try:
            test_index.add(t.read_text(encoding="utf-8"))
        except Exception:
            pass
    missing: list[str] = []
    for path in changed:
        if not path.startswith("studio/"):
            continue
        module = path.replace("/", ".").removesuffix(".py")
        leaf = module.rsplit(".", 1)[-1]
        covered = any(leaf in content for content in test_index)
        if not covered:
            missing.append(path)
    assert not missing, f"改了生產碼卻沒對應測試覆蓋（禁止『無測試的破壞性變更』）：{missing}"


def test_repro_health_checks():
    r = subprocess.run(
        ["python3", "-m", "ruff", "check", "."],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert r.returncode == 0, f"ruff 紅了，與文件宣稱不符：\n{r.stdout}{r.stderr}"
    # (b) pytest collect
    r = subprocess.run(
        ["python3", "-m", "pytest", "--collect-only", "-q"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert r.returncode == 0, f"pytest collect 紅了：\n{r.stdout}{r.stderr}"
    assert "tests collected" in r.stdout or "tests collected" in r.stderr, (
        "pytest collect 沒回報任何測試"
    )
