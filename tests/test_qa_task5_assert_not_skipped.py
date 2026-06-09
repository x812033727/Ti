"""QA 任務#5：job 結束的驗證步驟須確認沙箱測試「確實執行」而非被 skip。

對應 ci.yml 的「Run sandbox tests (and assert NOT skipped)」步驟：
  步驟A collect-only 取得預期選中數 EXPECTED
  步驟B 實跑 → out
  步驟C 四道閘門：errors / failed / skipped 任一出現即 exit 1；
        且實際 passed 數須 == EXPECTED（防漏選/部分沒跑而假綠）

本測試：
  (結構) 步驟存在、無 continue-on-error、含 collect-only 與四道閘門、選對檔案與條目。
  (行為) 抽出 ci.yml 內真實斷言區塊，注入各種 pytest 摘要字串，驗 exit code：
         全 PASSED 且數量相符→0；skipped/failed/errors/數量不符→1。
"""

import subprocess

import pytest
from _repo import REPO_ROOT

yaml = pytest.importorskip("yaml")

CI = REPO_ROOT / ".github" / "workflows" / "ci.yml"


@pytest.fixture(scope="module")
def step():
    d = yaml.safe_load(CI.read_text())
    steps = d["jobs"]["sandbox-test"]["steps"]
    hit = [s for s in steps if "assert not skipped" in s.get("name", "").lower()]
    assert hit, "找不到『assert NOT skipped』驗證步驟"
    return hit[0]


# --- 結構 ---------------------------------------------------------------


def test_step_exists_and_no_continue_on_error(step):
    assert step.get("continue-on-error") is not True, "驗證步驟不可 continue-on-error"


def test_step_env_sandbox_on(step):
    env = step.get("env", {})
    assert env.get("TI_SANDBOX") == "1"
    assert env.get("TI_SANDBOX_NET") == "1"  # 繞開 EPERM 的正確值


def test_step_has_collect_and_four_gates(step):
    run = step["run"]
    assert "--collect-only" in run, "缺 collect-only 取得預期數"
    assert "errors?" in run, "缺 error 閘門"
    assert "failed" in run, "缺 failed 閘門"
    assert "skipped" in run, "缺 skipped 閘門（任務#5 核心）"
    assert "passed" in run and "EXPECTED" in run and "ACTUAL" in run, "缺數量比對閘門"


def test_step_targets_right_tests(step):
    run = step["run"]
    assert "tests/test_runner.py" in run
    assert "tests/test_qa_task3_autopilot_pytest_exec.py" in run
    for nodeid in (
        "test_run_command_exec_sandbox_writes_cwd",
        "test_git_commit_three_steps_go_through_sandbox",
        "test_git_commit_no_injection_in_real_sandbox",
    ):
        assert nodeid in run, f"選擇器缺 {nodeid}"


# --- 行為：抽出 ci.yml 真實斷言區塊，注入摘要字串驗 exit code -----------


def _extract_decision_block(run: str) -> str:
    """抽出步驟C的判斷邏輯（從第一道 errors 閘門到結尾），供注入 out/EXPECTED 測試。"""
    lines = run.splitlines()
    start = next(i for i, ln in enumerate(lines) if 'grep -qiE "[0-9]+ errors?"' in ln)
    # 往前包含該 if 的開頭（start 行本身就是 `if echo "$out" | grep ...`）
    return "\n".join(lines[start:])


def _run_gate(decision: str, out: str, expected: str):
    """以注入的 out/EXPECTED 跑真實斷言區塊，回傳 (exit_code, stdout)。"""
    script = f"set -o pipefail\nEXPECTED={expected}\nout={out!r}\n{decision}\n"
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    return r.returncode, r.stdout


@pytest.fixture(scope="module")
def decision(step):
    return _extract_decision_block(step["run"])


def test_gate_passes_on_full_pass(decision):
    """全 PASSED 且數量相符 → exit 0。"""
    out = "....\n5 passed, 54 deselected in 0.80s"
    code, sout = _run_gate(decision, out, "5")
    assert code == 0, f"全綠應 exit 0：{sout}"
    assert "全數 PASSED" in sout


def test_gate_fails_on_skipped(decision):
    """有 skipped → exit 1（任務#5 核心：被 skip 不算通過）。"""
    out = "..s.\n4 passed, 1 skipped, 54 deselected in 0.80s"
    code, sout = _run_gate(decision, out, "5")
    assert code == 1, "出現 skipped 必須 exit 1"
    assert "被 skip" in sout


def test_gate_fails_on_failed(decision):
    out = "..F.\n4 passed, 1 failed in 0.80s"
    code, sout = _run_gate(decision, out, "5")
    assert code == 1, "出現 failed 必須 exit 1"
    assert "FAILED" in sout


def test_gate_fails_on_errors(decision):
    """collection error → exit 1（測試根本沒跑，不可放行）。"""
    out = "1 error in 0.10s"
    code, sout = _run_gate(decision, out, "5")
    assert code == 1, "出現 error 必須 exit 1"
    assert "error" in sout.lower()


def test_gate_fails_on_count_mismatch(decision):
    """數量不符（漏選/部分沒跑卻有零星 passed）→ exit 1。"""
    out = "...\n3 passed, 54 deselected in 0.50s"
    code, sout = _run_gate(decision, out, "5")
    assert code == 1, "passed 數 < 預期必須 exit 1"
    assert "數量不符" in sout


def test_gate_fails_when_no_passed(decision):
    """完全沒有 passed（且無 skip/fail/error 字樣）→ ACTUAL 空 → exit 1。"""
    out = "no tests ran in 0.01s"
    code, sout = _run_gate(decision, out, "5")
    assert code == 1, "無 passed 必須 exit 1"
