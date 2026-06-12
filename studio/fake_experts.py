"""離線示範用的假專家。

不呼叫 LLM，但會做「真實」動作：工程師/QA 真的把檔案寫進 workspace，讓 orchestrator
後續的 smoke-run、git commit、最終 Demo 都實際執行。用於無金鑰試用與端到端驗證。

示範情境是一個多任務、多檔案的真實小專案（四則運算 CLI）：工程師逐任務寫出
calculator.py / main.py / README.md，驗證工程師補上 pytest，最終 Demo 真的算出結果。
"""

from __future__ import annotations

import asyncio
import re
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


# --- 並行示範專案（多支線）：兩個獨立模組 + 一個依賴它們的整合說明 -------
# 設計成「波次 + 依賴」：#1 加法、#2 減法彼此獨立（第一波並行兩條 lane）；#3 整合說明
# 依賴 #1/#2（第二波，其 worktree 從前一波已合併的 HEAD 分支，看得到 add.py/sub.py）。
_ADD_PY = '"""加法模組。"""\n\n\ndef add(a, b):\n    return a + b\n'
_SUB_PY = '"""減法模組。"""\n\n\ndef sub(a, b):\n    return a - b\n'
_TEST_ADD_PY = "from add import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n"
_TEST_SUB_PY = "from sub import sub\n\n\ndef test_sub():\n    assert sub(5, 2) == 3\n"
_README_PARALLEL_MD = """\
# 四則運算模組

由 Ti Studio 離線「並行」示範產生：兩個獨立模組分波並行，整合說明依賴它們。

- `add.py` — 加法（任務 #1）
- `sub.py` — 減法（任務 #2）

```bash
python -m pytest -q   # 兩模組測試全過
```
"""

# 任務 id → 該支線要寫出的檔案（工程師在自己的 worktree 寫，互不重疊以利合併）。
_PARALLEL_FILES: dict[int, dict[str, str]] = {
    1: {"add.py": _ADD_PY, "test_add.py": _TEST_ADD_PY},
    2: {"sub.py": _SUB_PY, "test_sub.py": _TEST_SUB_PY},
    3: {"README.md": _README_PARALLEL_MD},
}


def _task_id_from_cwd(cwd: Path) -> int:
    """從 lane 的 worktree 目錄名（"task-<id>[-<id>...]"）解析出任務 id；解析不到回 0。"""
    m = re.search(r"task-(\d+)", Path(cwd).name)
    return int(m.group(1)) if m else 0


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


def build_fake_critics(session_id: str, cwd: Path) -> dict[str, FakeExpert]:
    """離線示範用的異議檢查者（critic）：獨立實例、不寫檔，固定『不成立』放行。

    對應「換人」原則：任務審查 gate 用 pm 視角、最終驗收 gate 用 senior 視角。
    這讓離線端到端流程能跑完、又展示至少一次「內部討論」事件（critic_review）。
    """
    return {
        "pm": FakeExpert(
            BY_KEY["pm"],
            session_id,
            cwd,
            scripts=["以驗收標準逐項檢查，找不到實質問題。\n異議: 不成立"],
        ),
        "senior": FakeExpert(
            BY_KEY["senior"],
            session_id,
            cwd,
            scripts=["整體交付對齊需求與驗收標準，沒有實質反對。\n異議: 不成立"],
        ),
    }


def build_fake_lane_expert(role: Role, session_id: str, cwd: Path) -> FakeExpert:
    """並行示範用的 lane 專家工廠：每條支線各一套，工程師依其 worktree 對應的任務寫檔。

    供 orchestrator 的 `_lane_expert_factory` 注入（離線 + 並行時）。工程師寫該任務的檔、
    驗證工程師回 PASS、其餘角色給通用台詞；critic 在離線一律放行（見 _get_critic）。
    """
    tid = _task_id_from_cwd(cwd)
    if role.key == "engineer":
        return FakeExpert(
            role,
            session_id,
            cwd,
            scripts=["已完成本支線任務，並在自己的 worktree 跑過確認可執行。"],
            action_marker="任務 #",
            file_queue=[_PARALLEL_FILES.get(tid, {})],
        )
    if role.key == "qa":
        return FakeExpert(
            role, session_id, cwd, scripts=["在本支線重跑測試，全數通過。\n驗證: PASS"]
        )
    if role.key == "senior":
        return FakeExpert(
            role, session_id, cwd, scripts=["本支線程式碼分層清楚、無明顯問題。\n決議: 核可"]
        )
    return FakeExpert(role, session_id, cwd, scripts=["以驗收標準逐項檢查，沒有實質反對。"])


def _pm_decompose_script(requirement: str) -> str:
    """PM 拆解台詞：並行模式宣告含依賴的波次任務，循序模式維持原本四則運算 CLI。"""
    if config.PARALLEL_TASKS_ENABLED:
        return (
            f"收到需求：{requirement}。我拆成可並行的模組任務（獨立者分波同時做）。\n"
            "任務: #1 實作加法模組 add.py\n"
            "任務: #2 實作減法模組 sub.py\n"
            "任務: #3 補整合說明 README（彙整 add/sub）\n"
            "依賴: #3 -> #1\n"
            "依賴: #3 -> #2\n"
            "驗收標準: add/sub 模組正確且各有測試；整合說明列出兩模組\n"
            "執行指令: python -m pytest -q"
        )
    # 循序示範同時宣告議程子題＋主責（任務 #3 的疊加格式），讓離線冒煙能走完
    # 「需求→議程拆解→分派→逐子題討論→彙整」全流程。第二子題刻意指派本場缺席的
    # architect：現場實測 validate_assignees 硬驗證 fallback（修正記入 agenda_plan
    # 事件的 corrections），這正是「絕不讓 LLM 即興分派直通」的展示。
    # （並行示範腳本維持原樣：並行離線 e2e 釘的是波次/lane 語義，不疊議程。）
    return (
        f"收到需求：{requirement}。我先列議程子題並指派主責，再拆成三個任務循序完成。\n"
        "子題: 核心運算模組 | 設計 calculator.py 的四則運算與除零錯誤處理 | 四則正確、除以 0 報錯\n"
        "負責: engineer\n"
        "子題: 介面與說明 | 設計 main.py 命令列參數與 README 用法 | 一行指令可算出結果\n"
        "負責: architect\n"
        "任務: 實作四則運算核心 calculator.py\n"
        "任務: 建立命令列介面 main.py\n"
        "任務: 補上 README 使用說明\n"
        "驗收標準: calculator 四則運算正確、除以 0 報錯；main.py 可由命令列執行\n"
        "執行指令: python main.py add 3 4"
    )


def build_fake_experts(session_id: str, cwd: Path, requirement: str) -> dict[str, FakeExpert]:
    return {
        "pm": FakeExpert(
            BY_KEY["pm"],
            session_id,
            cwd,
            scripts=[
                _pm_decompose_script(requirement),
                "所有任務都完成，測試通過、Demo 可執行。\n決議: 完成",
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
