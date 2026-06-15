"""工程師守護：三閘門「標籤計入截尾預算」一致化（任務 #3 第 2 輪）。

高工審查指出：`_gate_tests` 扣除前綴維持總長 ≤1500，但 `_gate_lint`/`_gate_collect`
原本直接前綴、會溢位上限。本檔鎖死三閘門一致行為——超長輸出時：
  1. 回傳字串開頭帶對應層級標籤（`[lint]`/`[collect]`/`[test]`）；
  2. 帶標籤後總長不超過該閘門的截尾上限（lint/collect=1200、test=1500）；
  3. 尾段（最關鍵的錯誤尾巴）保留，未被前綴擠掉。
任一條被改回「先前綴後截尾不扣預算」即紅。
"""

from __future__ import annotations

import pytest

from studio import autopilot, runner
from studio.runner import RunOutput


class _SpyByLabel:
    """依 label 回傳指定 (exit, output)；未指定者預設 ok 空輸出。"""

    def __init__(self, results):
        self.results = results

    async def __call__(self, cwd, argv, timeout=None, sandbox=None, label=None):
        exit_code, output = self.results.get(label, (0, ""))
        return RunOutput(command=label or "", exit_code=exit_code, output=output, timed_out=False)


# 遠超上限的輸出，尾端放可辨識 marker 驗證尾段保留。
_LONG = "x" * 5000 + "TAIL_MARKER"


@pytest.mark.asyncio
async def test_lint_failure_label_within_budget(monkeypatch):
    spy = _SpyByLabel({"ruff probe": (0, "ruff 0.x"), "ruff check": (1, _LONG)})
    monkeypatch.setattr(runner, "run_command_exec", spy)
    ok, out = await autopilot._gate_lint("/c")
    assert ok is False
    assert out.startswith("[lint] "), f"lint 失敗漏標籤：{out[:20]!r}"
    assert len(out) <= 1200, f"lint 帶標籤總長爆量：{len(out)}"
    assert out.endswith("TAIL_MARKER"), "lint 尾段被前綴擠掉"


@pytest.mark.asyncio
async def test_collect_failure_label_within_budget(monkeypatch):
    spy = _SpyByLabel({"collect (no SDK)": (2, _LONG)})
    monkeypatch.setattr(runner, "run_command_exec", spy)
    ok, out = await autopilot._gate_collect_without_sdk("/c")
    assert ok is False
    assert out.startswith("[collect] "), f"collect 失敗漏標籤：{out[:20]!r}"
    assert len(out) <= 1200, f"collect 帶標籤總長爆量：{len(out)}"
    assert out.endswith("TAIL_MARKER"), "collect 尾段被前綴擠掉"


@pytest.mark.asyncio
async def test_tests_failure_label_within_budget(monkeypatch):
    spy = _SpyByLabel({"pytest gate": (1, _LONG)})
    monkeypatch.setattr(runner, "run_command_exec", spy)
    ok, out = await autopilot._gate_tests("/c")
    assert ok is False
    assert out.startswith("[test] "), f"test 失敗漏標籤：{out[:20]!r}"
    assert len(out) <= 1500, f"test 帶標籤總長爆量：{len(out)}"
    assert out.endswith("TAIL_MARKER"), "test 尾段被前綴擠掉"
