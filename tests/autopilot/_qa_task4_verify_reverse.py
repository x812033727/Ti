"""QA 驗收 #4：反向黑樣本實證——抽掉標籤必須紅。

不修改源碼，僅以 monkeypatch 動態改 autopilot._gate_tests 的實作（截掉 [test] 前綴），
跑同樣的 startswith 斷言，預期 fail。證明 test_qa_task3b_gate_level_labels.py 的反向斷言
有真實判別力，不是「寫死字串、恆真」。

本檔案不是合約測試，純粹是 QA 在交付前的手動破壞性驗證，跑完即丟。
"""

from __future__ import annotations

import pytest

from studio import autopilot, runner
from studio.runner import RunOutput


class _Spy:
    def __init__(self):
        self.calls = []

    async def __call__(self, cwd, argv, timeout=None, sandbox=None, label=None):
        self.calls.append({"label": label})
        return RunOutput(
            command=label or "", exit_code=1, output="1 failed in foo", timed_out=False
        )


async def test_sabotage_remove_prefix_goes_red(monkeypatch):
    """sabotage：把 _gate_tests 的前綴拿掉，斷言應 fail。

    若此測試 PASS（沒被破壞），代表斷言寫死或偽綠——QA 必須立刻回報。
    """
    monkeypatch.setattr(runner, "run_command_exec", _Spy())

    # 用 monkeypatch 動態替換 _gate_tests 為「不帶前綴」版本
    async def sabotaged_gate_tests(clone):
        r = await runner.run_command_exec(clone, [], label="pytest gate")
        return r.ok, r.output  # ← 故意不 prepend "[test] "

    monkeypatch.setattr(autopilot, "_gate_tests", sabotaged_gate_tests)

    ok, out = await autopilot._gate_tests("/c")
    # 破壞點：斷言 prefix 必須落在開頭，sabotage 後開頭是 "1 failed..."，必紅
    assert out.startswith("[test] "), f"sabotage 應使斷言紅：{out!r}"
