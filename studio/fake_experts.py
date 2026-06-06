"""離線示範用的假專家。

不呼叫 LLM，但會做「真實」動作：工程師/QA 真的把檔案寫進 workspace，讓 orchestrator
後續的 smoke-run、git commit、最終 Demo 都實際執行。用於無金鑰試用與端到端驗證。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from . import config, events
from .roles import BY_KEY, Role

# 範例成果：一個真的能執行的小程式 + 測試
_MAIN_PY = '''\
def greet(name="world"):
    return f"Hello, {name}!"


if __name__ == "__main__":
    print(greet("Ti Studio"))
'''

_TEST_PY = '''\
from main import greet


def test_greet_default():
    assert greet() == "Hello, world!"


def test_greet_name():
    assert greet("Ti") == "Hello, Ti!"
'''


class FakeExpert:
    """依角色腳本回應；可在第一次發言時把檔案寫進 workspace。"""

    def __init__(self, role: Role, session_id: str, cwd: Path,
                 scripts: list[str], files: dict[str, str] | None = None):
        self.role = role
        self.session_id = session_id
        self._cwd = cwd
        self._scripts = scripts
        self._files = files or {}
        self._written = False
        self.calls = 0

    async def speak(self, prompt: str, broadcast) -> str:
        r = self.role
        await broadcast(events.expert_status(self.session_id, r.key, "thinking"))
        if config.OFFLINE_DELAY:
            await asyncio.sleep(config.OFFLINE_DELAY)

        if self._files and not self._written:
            await broadcast(events.expert_status(self.session_id, r.key, "working"))
            for name, content in self._files.items():
                (self._cwd / name).write_text(content, encoding="utf-8")
                await broadcast(events.tool_use(self.session_id, r.key, "Write", f"寫入 {name}"))
            self._written = True

        text = self._scripts[min(self.calls, len(self._scripts) - 1)]
        self.calls += 1
        await broadcast(events.expert_message(self.session_id, r.key, r.name, r.avatar, text))
        await broadcast(events.expert_status(self.session_id, r.key, "idle"))
        return text

    async def stop(self) -> None:
        pass


def build_fake_experts(session_id: str, cwd: Path, requirement: str) -> dict[str, FakeExpert]:
    return {
        "pm": FakeExpert(BY_KEY["pm"], session_id, cwd, scripts=[
            "任務: 實作範例程式\n"
            "驗收標準: 能執行並輸出問候語\n"
            "執行指令: python main.py",
            "成果符合驗收標準。\n決議: 完成",
            "做得不錯，下次可加更多測試與輸入處理。",
        ]),
        "engineer": FakeExpert(BY_KEY["engineer"], session_id, cwd, scripts=[
            "我打算用一個 greet() 函式，main.py 直接可執行。",
            "已建立 main.py，可用 `python main.py` 執行。",
        ], files={"main.py": _MAIN_PY}),
        "qa": FakeExpert(BY_KEY["qa"], session_id, cwd, scripts=[
            "已加入 test_main.py 覆蓋預設與帶名稱情況，測試全過。\n驗證: PASS",
        ], files={"test_main.py": _TEST_PY}),
        "senior": FakeExpert(BY_KEY["senior"], session_id, cwd, scripts=[
            "結構清楚，命名合理，沒有明顯問題。",
            "品質良好，可接受。\n決議: 核可",
        ]),
    }
