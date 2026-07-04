"""前端向後相容性：handleEvent 對未知事件與新事件不崩潰，且 switch 無 default。

新事件（huddle／critic_review）採自由 dict payload；前端依賴 switch 無 default，
未知事件天然被忽略。這裡用 node 實際載入 web/js/events-render.js（ES module）
並執行 handleEvent 驗證。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from _repo import REPO_ROOT

_ROOT = REPO_ROOT
_APP_JS = _ROOT / "web" / "js" / "events-render.js"
_SMOKE = Path(__file__).resolve().parent / "frontend_handleevent_smoke.mjs"


def _extract_function(src: str, marker: str) -> str:
    """以大括號配對計數器抓出 ``marker`` 起始的函式體（比行號/下一函式邊界穩健）。"""
    idx = src.index(marker)
    open_brace = src.index("{", idx)
    depth = 0
    for i in range(open_brace, len(src)):
        ch = src[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return src[idx : i + 1]
    raise AssertionError(f"{marker} 大括號不配對")


def test_handleevent_switch_has_no_default():
    """handleEvent 的 switch 不得有 default 分支（未知事件才會被天然忽略）。"""
    src = _APP_JS.read_text(encoding="utf-8")
    body = _extract_function(src, "function handleEvent")
    assert "switch (ev.type)" in body
    assert "default:" not in body
    assert "ev.payload || {}" in body  # payload 防呆


def test_every_event_type_has_frontend_case():
    """每個後端 EventType 在前端都有對應 case（含新增的 huddle／critic_review）。"""
    from studio.events import EventType

    src = _APP_JS.read_text(encoding="utf-8")
    missing = [e.value for e in EventType if f'case "{e.value}"' not in src]
    assert not missing, f"前端 handleEvent 缺少這些事件的 case：{missing}"


@pytest.mark.skipif(shutil.which("node") is None, reason="需要 node 執行前端 smoke")
def test_handleevent_does_not_crash_on_unknown_and_new_events():
    result = subprocess.run(
        ["node", str(_SMOKE)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, f"前端 smoke 失敗：\n{result.stdout}\n{result.stderr}"
    assert "OK" in result.stdout
