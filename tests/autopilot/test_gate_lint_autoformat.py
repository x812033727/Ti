"""lint 閘門自動格式化（#249）：`ruff format --check` 紅時先寫回排版再重驗，不再整場退回。

背景：autopilot 任務 #249 連續三輪（每輪 1-2 小時）卡在同一道牆——專家產碼後 pytest 全綠，
卻因純格式漂移（studio/appraisal.py 需 reformat）被 lint 閘門退回重試，為空格燒掉整場討論。
格式是機器可修的確定性問題，閘門應自己修掉再重驗。

驗收矩陣（含黑樣本，證明判別力）：
1. format --check 紅 → 自動 `ruff format` 寫回 → 重驗綠 → 閘門通過（不退回），log 繁中
   「格式已自動修正 N 檔」。
2. 自動修後重驗仍紅 → 維持原退回行為（False + [lint] 前綴）。
3. TI_LINT_AUTOFORMAT=0（config.LINT_AUTOFORMAT False）→ 完全不跑寫回，直接退回（舊行為）。
4. `ruff check`（語意 lint）紅 → 不受影響照退，且絕不觸發寫回（自動修復僅限純排版）。
5. config 旋鈕：預設開啟、進 reload()、env 可關。
6. 寫回落點守護：run_one_task 中 _gate_lint 先於 _commit_push_merge，且後者以 `git add -A`
   兜底 commit——自動格式化的寫回會被後續 commit 自然帶上。
"""

from __future__ import annotations

import ast
import inspect
import logging
import os
import subprocess
import sys

from studio import autopilot, config, runner
from studio.runner import RunOutput

_FMT_CHECK = "ruff format --check"
_FMT_WRITE = "ruff format"


class SeqSpy:
    """依 label 依序回放結果：同一 label 第 N 次呼叫取第 N 個結果，序列耗盡／未指定預設 ok。

    比 test_qa_task3b_gate_level_labels.SpyByLabel 多了「同 label 兩次呼叫可回不同結果」，
    才能模擬「--check 第一次紅、寫回後重驗綠」的時序。記錄全部呼叫供 argv/次數斷言。
    """

    def __init__(self, results: dict[str, list[tuple[int, str]]] | None = None):
        self.results = {k: list(v) for k, v in (results or {}).items()}
        self.calls: list[dict] = []

    async def __call__(self, cwd, argv, timeout=None, sandbox=None, label=None):
        self.calls.append({"cwd": cwd, "argv": list(argv), "label": label})
        seq = self.results.get(label)
        exit_code, output = seq.pop(0) if seq else (0, "")
        return RunOutput(command=label or "", exit_code=exit_code, output=output, timed_out=False)

    def labels(self) -> list[str]:
        return [c["label"] for c in self.calls]


def _write_calls(spy: SeqSpy) -> list[dict]:
    """取出「寫回模式」的 ruff format 呼叫（label 為 ruff format 且 argv 不含 --check）。"""
    return [c for c in spy.calls if c["label"] == _FMT_WRITE and "--check" not in c["argv"]]


# ---------------------------------------------------------------------------
# 1) format 紅 → 自動修 → 重驗綠 → 通過（不退回）
# ---------------------------------------------------------------------------


async def test_format_fail_autofix_then_pass(monkeypatch, caplog):
    spy = SeqSpy(
        {
            _FMT_CHECK: [
                (1, "Would reformat: studio/appraisal.py\n1 file would be reformatted"),
                (0, "119 files already formatted"),  # 寫回後重驗綠
            ],
            _FMT_WRITE: [(0, "1 file reformatted, 118 files left unchanged")],
        }
    )
    monkeypatch.setattr(runner, "run_command_exec", spy)
    monkeypatch.setattr(config, "LINT_AUTOFORMAT", True)
    with caplog.at_level(logging.INFO, logger="ti.autopilot"):
        ok, out = await autopilot._gate_lint("/c")
    assert ok is True, f"純格式漂移應被自動修掉、閘門放行：{out!r}"
    assert out.startswith("[lint] "), "成功路徑仍須帶層級標籤"
    # 寫回模式確實跑過，且 argv 是 `ruff format .`（無 --check）、同一工作區
    writes = _write_calls(spy)
    assert len(writes) == 1, f"應恰跑一次寫回：{spy.labels()}"
    assert writes[0]["argv"] == [sys.executable, "-m", "ruff", "format", "."]
    assert writes[0]["cwd"] == "/c", "寫回必須發生在同一工作區（後續 commit 才帶得上）"
    # 時序：check 紅 → 寫回 → 重驗
    assert spy.labels() == ["ruff probe", "ruff check", _FMT_CHECK, _FMT_WRITE, _FMT_CHECK]
    # 繁中一行 log：「格式已自動修正 N 檔」（N 取自寫回輸出的 1 file reformatted）
    assert "格式已自動修正 1 檔" in caplog.text, f"缺自動修正 log：{caplog.text!r}"


# ---------------------------------------------------------------------------
# 2) 自動修後重驗仍紅 → 維持原退回行為（黑樣本：autofix 不是無條件放行）
# ---------------------------------------------------------------------------


async def test_autofix_recheck_still_failing_falls_back(monkeypatch):
    spy = SeqSpy(
        {
            _FMT_CHECK: [(1, "would reformat a.py"), (1, "would reformat a.py STILLRED")],
            _FMT_WRITE: [(0, "")],
        }
    )
    monkeypatch.setattr(runner, "run_command_exec", spy)
    monkeypatch.setattr(config, "LINT_AUTOFORMAT", True)
    ok, out = await autopilot._gate_lint("/c")
    assert ok is False, "重驗仍紅必須維持退回，autofix 不得變成無條件放行"
    assert out.startswith("[lint] ruff format --check 未過"), f"退回訊息走原格式：{out!r}"
    assert "STILLRED" in out, "退回訊息應取重驗（最新一次）輸出"
    assert len(_write_calls(spy)) == 1, "寫回只試一次，不無限重試"


# ---------------------------------------------------------------------------
# 3) 旋鈕關閉 → 直接退回（舊行為），完全不碰工作區
# ---------------------------------------------------------------------------


async def test_knob_off_restores_old_behavior(monkeypatch):
    spy = SeqSpy({_FMT_CHECK: [(1, "would reformat a.py")]})
    monkeypatch.setattr(runner, "run_command_exec", spy)
    monkeypatch.setattr(config, "LINT_AUTOFORMAT", False)
    ok, out = await autopilot._gate_lint("/c")
    assert ok is False
    assert out.startswith("[lint] ruff format --check 未過")
    assert _write_calls(spy) == [], "旋鈕關閉時絕不可寫回工作區"
    assert spy.labels() == ["ruff probe", "ruff check", _FMT_CHECK], "不得有任何額外重驗"


# ---------------------------------------------------------------------------
# 4) ruff check（語意 lint）紅 → 不受影響照退，且不觸發寫回
# ---------------------------------------------------------------------------


async def test_ruff_check_failure_unaffected(monkeypatch):
    spy = SeqSpy({"ruff check": [(1, "F401 unused import")]})
    monkeypatch.setattr(runner, "run_command_exec", spy)
    monkeypatch.setattr(config, "LINT_AUTOFORMAT", True)
    ok, out = await autopilot._gate_lint("/c")
    assert ok is False
    assert out.startswith("[lint] ruff check 未過"), f"語意 lint 失敗訊息不變：{out!r}"
    assert _write_calls(spy) == [], "語意 lint 失敗絕不可觸發自動格式化（不動程式邏輯）"
    assert spy.labels() == ["ruff probe", "ruff check"], "check 紅即止，不繼續跑 format"


# ---------------------------------------------------------------------------
# 5) config 旋鈕：預設開啟、進 reload()、env 可關
# ---------------------------------------------------------------------------


def test_knob_reload_roundtrip(monkeypatch):
    monkeypatch.setenv("TI_LINT_AUTOFORMAT", "0")
    config.reload()
    assert config.LINT_AUTOFORMAT is False, "TI_LINT_AUTOFORMAT=0 應經 reload() 生效"
    monkeypatch.delenv("TI_LINT_AUTOFORMAT")
    config.reload()
    assert config.LINT_AUTOFORMAT is True, "未設 env 時預設開啟"


def test_knob_default_on_in_isolated_env(tmp_path):
    """乾淨子程序環境（無 TI_* 覆寫、無 .env）驗證預設值，仿 test_selfimprove_config 範式。"""
    repo_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    env = {k: v for k, v in os.environ.items() if not k.startswith("TI_")}
    env["PYTHONPATH"] = repo_root
    r = subprocess.run(
        [sys.executable, "-c", "import studio.config as c; print(c.LINT_AUTOFORMAT)"],
        capture_output=True,
        text=True,
        env=env,
        cwd=tmp_path,  # 避免讀到 repo 的 .env
    )
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "True"


# ---------------------------------------------------------------------------
# 6) 寫回落點守護：gate 先於 commit，且 commit 以 git add -A 兜底
# ---------------------------------------------------------------------------


def test_autoformat_writes_land_in_followup_commit():
    """自動格式化的寫回必須被後續 commit 自然帶上：run_one_task 先呼叫 _gate_lint、後呼叫
    _commit_push_merge，而 _commit_push_merge 以 `git add -A` 收攏工作區全部變更。
    任一環節被重排（gate 移到 commit 後／add -A 被拿掉）此守護即紅。
    以 AST 抓「實際呼叫」順序（非字串比對，避免被註解/docstring 提及誤中）。"""
    src = inspect.getsource(autopilot.run_one_task)
    fn = ast.parse(src).body[0]
    call_lines: dict[str, int] = {}
    for node in ast.walk(fn):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            call_lines.setdefault(node.func.id, node.lineno)  # 取首次呼叫的行號
    assert {"_gate_lint", "_commit_push_merge"} <= call_lines.keys(), sorted(call_lines)
    assert call_lines["_gate_lint"] < call_lines["_commit_push_merge"], (
        f"lint 閘門必須先於 commit，自動格式化寫回才會被帶上：{call_lines}"
    )
    commit_src = inspect.getsource(autopilot._commit_push_merge)
    assert '"add", "-A"' in commit_src, "_commit_push_merge 需以 git add -A 兜底收攏寫回"


def test_reformat_count_parses_both_sources():
    """檔數解析：優先寫回輸出的「N files reformatted」，退而數 --check 的 Would reformat 行。"""
    assert autopilot._reformat_count("3 files reformatted, 10 files left unchanged", "") == 3
    assert autopilot._reformat_count("1 file reformatted", "") == 1
    assert autopilot._reformat_count("", "Would reformat: a.py\nWould reformat: b.py\ndone") == 2
    assert autopilot._reformat_count("", "") == 0
