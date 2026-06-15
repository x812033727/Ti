"""任務 #4：守護 CI `test` job 的「collect 與 test 分離」不被悄悄改回單步。

對應 ci.yml `jobs.test.steps` 的兩個步驟：
  - "Collect tests"：`python -m pytest --collect-only -q`，先只收集不執行；
    collection error（exit 2）在此獨立失敗，不與測試失敗（exit 1）糊在同一 step。
  - "Run tests"：完整 `python -m pytest`，置於 collect 步驟之後。

守護內容（依架構決策）：
  (a) `jobs['test']['steps']` 內存在獨立 collect 步驟（以 step name 定位，不全檔掃
      `--collect-only`——sandbox-test job 也含該旗標，全檔掃會被它假綠）。
  (b) collect 步驟順序在 run 步驟之前（以 name 搜尋取 index 比大小，不硬寫常數，
      日後在兩者間插入其他 step 也不假綠）。
  (c) collect 與 run 步驟皆不帶 `continue-on-error: true`（否則靜默吞掉 exit 2/1，
      獨立失敗中斷能力形同虛設）。
  (d) collect 步驟帶 `--collect-only`；run 步驟為完整 pytest 且**不**帶 `--collect-only`。

判別力（排假綠）：`test_single_step_revert_is_red` 餵一個「collect 與 test 合併為單一
step」的合成 yaml，斷言同一套檢查邏輯會轉紅——證明改回單步真的會被攔下，而非永遠放行。
"""

import pytest
from _repo import REPO_ROOT

yaml = pytest.importorskip("yaml")

CI = REPO_ROOT / ".github" / "workflows" / "ci.yml"


def _test_steps(doc):
    return doc["jobs"]["test"]["steps"]


def _find_idx(steps, needle):
    """以 step name 子字串（不分大小寫）定位 index；找不到回 -1。"""
    for i, s in enumerate(steps):
        if needle in s.get("name", "").lower():
            return i
    return -1


def _violations(steps):
    """回傳「分離不成立」的問題清單；空清單＝結構正確。供正反樣本共用同一套邏輯。"""
    problems = []
    ci = _find_idx(steps, "collect tests")
    ri = _find_idx(steps, "run tests")
    if ci < 0:
        problems.append("缺獨立『Collect tests』步驟（疑似已併回單步）")
    if ri < 0:
        problems.append("缺『Run tests』步驟")
    if ci >= 0 and ri >= 0:
        if ci >= ri:
            problems.append(f"collect 步驟須在 run 之前（collect={ci}, run={ri}）")
        collect, run = steps[ci], steps[ri]
        if "--collect-only" not in collect.get("run", ""):
            problems.append("collect 步驟缺 --collect-only")
        if collect.get("continue-on-error") is True:
            problems.append("collect 步驟不可 continue-on-error")
        if "--collect-only" in run.get("run", ""):
            problems.append("run 步驟不應帶 --collect-only（那是 collect 步驟的事）")
        if "pytest" not in run.get("run", ""):
            problems.append("run 步驟須執行完整 pytest")
        if run.get("continue-on-error") is True:
            problems.append("run 步驟不可 continue-on-error")
    return problems


@pytest.fixture(scope="module")
def steps():
    return _test_steps(yaml.safe_load(CI.read_text()))


# --- 結構守護：現況須全綠 ------------------------------------------------


def test_collect_step_exists_independent(steps):
    assert _find_idx(steps, "collect tests") >= 0, "找不到獨立『Collect tests』步驟"


def test_collect_before_run(steps):
    ci = _find_idx(steps, "collect tests")
    ri = _find_idx(steps, "run tests")
    assert ci >= 0 and ri >= 0, "collect/run 步驟須同時存在"
    assert ci < ri, f"collect 步驟須排在 run 之前（collect={ci}, run={ri}）"


def test_collect_step_uses_collect_only(steps):
    ci = _find_idx(steps, "collect tests")
    run = steps[ci].get("run", "")
    assert "--collect-only" in run, "collect 步驟須帶 --collect-only"
    assert "pytest" in run, "collect 步驟須以 pytest 收集"


def test_run_step_is_full_pytest(steps):
    ri = _find_idx(steps, "run tests")
    run = steps[ri].get("run", "")
    assert "pytest" in run
    assert "--collect-only" not in run, "run 步驟不應帶 --collect-only（應分離給 collect 步驟）"


def test_neither_step_continue_on_error(steps):
    for needle in ("collect tests", "run tests"):
        s = steps[_find_idx(steps, needle)]
        assert s.get("continue-on-error") is not True, f"{needle} 不可 continue-on-error"


def test_real_ci_has_no_violations(steps):
    assert _violations(steps) == [], "現行 ci.yml test job 不應有分離違規"


# --- 判別力：改回單步即紅（合成黑樣本） ---------------------------------


def test_single_step_revert_is_red():
    """把 collect 與 test 併回單一 step 後，同一套檢查須轉紅，證明守護真有判別力。"""
    reverted = [
        {"name": "Set up Python", "uses": "actions/setup-python@v5"},
        # 單步：collect 與 run 糊在一起（collection error 與測試失敗無法分辨）
        {"name": "Run tests", "run": "python -m pytest -q --cov=studio"},
    ]
    problems = _violations(reverted)
    assert problems, "改回單步時守護必須轉紅，卻判為無違規（假綠）"
    assert any("collect" in p.lower() for p in problems), problems
