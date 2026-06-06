"""離線示範用的假專家。

不呼叫 LLM，但會做「真實」動作：工程師/QA 真的把檔案寫進 workspace，讓 orchestrator
後續的 smoke-run、git commit、最終 Demo 都實際執行。用於無金鑰試用與端到端驗證。

示範情境是一個多任務、多檔案的真實小專案（四則運算 CLI）：工程師逐任務寫出
calculator.py / main.py / README.md，驗證工程師補上 pytest，最終 Demo 真的算出結果。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from . import config, events
from .roles import BY_KEY, Role

# --- 範例專案檔案（逐任務寫出）----------------------------------------
_CALCULATOR_PY = '''\
"""四則運算核心。"""


def add(a, b):
    return a + b


def sub(a, b):
    return a - b


def mul(a, b):
    return a * b


def div(a, b):
    if b == 0:
        raise ValueError("除數不可為 0")
    return a / b
'''

_MAIN_PY = '''\
"""命令列介面：python main.py <add|sub|mul|div> <a> <b>"""

import sys

from calculator import add, sub, mul, div

OPS = {"add": add, "sub": sub, "mul": mul, "div": div}


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 3 or argv[0] not in OPS:
        print("用法: python main.py <add|sub|mul|div> <a> <b>")
        return 1
    op, a, b = argv[0], float(argv[1]), float(argv[2])
    print(OPS[op](a, b))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''

_README_MD = """\
# 四則運算 CLI

由 Ti Studio 離線示範產生的小專案。

## 用法

```bash
python main.py add 3 4   # -> 7.0
python main.py div 1 0   # -> 除數不可為 0
```
"""

_TEST_PY = """\
import pytest

from calculator import add, sub, mul, div


def test_basic_ops():
    assert add(2, 3) == 5
    assert sub(5, 2) == 3
    assert mul(3, 4) == 12
    assert div(10, 2) == 5


def test_div_zero():
    with pytest.raises(ValueError):
        div(1, 0)
"""


class FakeExpert:
    """依角色腳本回應；在「動作」型發言時把下一組檔案寫進 workspace。

    file_queue 內每一項是 {檔名: 內容}；當 prompt 含 action_marker 時，依序取出一組寫入。
    這樣工程師會「逐任務」寫出不同檔案，而辯論等非動作發言不會誤觸。
    """

    def __init__(
        self,
        role: Role,
        session_id: str,
        cwd: Path,
        scripts: list[str],
        file_queue: list[dict[str, str]] | None = None,
        action_marker: str = "",
    ):
        self.role = role
        self.session_id = session_id
        self._cwd = cwd
        self._scripts = scripts
        self._queue = list(file_queue or [])
        self._marker = action_marker
        self.calls = 0

    async def speak(self, prompt: str, broadcast) -> str:
        r = self.role
        await broadcast(events.expert_status(self.session_id, r.key, "thinking"))
        if config.OFFLINE_DELAY:
            await asyncio.sleep(config.OFFLINE_DELAY)

        if self._queue and self._marker and self._marker in prompt:
            await broadcast(events.expert_status(self.session_id, r.key, "working"))
            for name, content in self._queue.pop(0).items():
                (self._cwd / name).write_text(content, encoding="utf-8")
                await broadcast(events.tool_use(self.session_id, r.key, "Write", f"寫入 {name}"))

        text = self._scripts[min(self.calls, len(self._scripts) - 1)]
        self.calls += 1
        await broadcast(events.expert_message(self.session_id, r.key, r.name, r.avatar, text))
        await broadcast(events.expert_status(self.session_id, r.key, "idle"))
        return text

    async def stop(self) -> None:
        pass


def build_fake_experts(session_id: str, cwd: Path, requirement: str) -> dict[str, FakeExpert]:
    return {
        "pm": FakeExpert(
            BY_KEY["pm"],
            session_id,
            cwd,
            scripts=[
                f"收到需求：{requirement}。我拆成三個任務循序完成。\n"
                "任務: 實作四則運算核心 calculator.py\n"
                "任務: 建立命令列介面 main.py\n"
                "任務: 補上 README 使用說明\n"
                "驗收標準: calculator 四則運算正確、除以 0 報錯；main.py 可由命令列執行\n"
                "執行指令: python main.py add 3 4",
                "三個任務都完成，測試通過、Demo 可執行。\n決議: 完成",
                "做得不錯：模組分層清楚、有測試。下次可加更多輸入驗證與互動模式。",
            ],
        ),
        "engineer": FakeExpert(
            BY_KEY["engineer"],
            session_id,
            cwd,
            scripts=[
                "我建議分成核心模組 calculator.py 與介面 main.py，先核心再介面。",
                "已完成本任務，並自己跑過確認可執行。",
            ],
            action_marker="任務 #",
            file_queue=[
                {"calculator.py": _CALCULATOR_PY},
                {"main.py": _MAIN_PY},
                {"README.md": _README_MD},
            ],
        ),
        "qa": FakeExpert(
            BY_KEY["qa"],
            session_id,
            cwd,
            scripts=[
                "已加入 test_calculator.py 覆蓋四則運算與除零情況，測試全過。\n驗證: PASS",
                "重跑測試，仍全數通過。\n驗證: PASS",
            ],
            action_marker="撰寫並執行測試",
            file_queue=[
                {"test_calculator.py": _TEST_PY},
            ],
        ),
        "senior": FakeExpert(
            BY_KEY["senior"],
            session_id,
            cwd,
            scripts=[
                "分層合理，建議介面與核心分開，錯誤處理用例外。",
                "程式碼品質良好，命名清楚，沒有明顯問題。\n決議: 核可",
            ],
        ),
    }
