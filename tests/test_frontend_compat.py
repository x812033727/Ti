"""前端向後相容性：handleEvent 對未知事件與新事件不崩潰，且 switch 無 default。

新事件（huddle／critic_review）採自由 dict payload；前端依賴 switch 無 default，
未知事件天然被忽略。這裡用 node 實際載入 web/app.js 並執行 handleEvent 驗證。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_APP_JS = _ROOT / "web" / "app.js"
_SMOKE = Path(__file__).resolve().parent / "frontend_handleevent_smoke.mjs"


def test_handleevent_switch_has_no_default():
    """handleEvent 的 switch 不得有 default 分支（未知事件才會被天然忽略）。"""
    src = _APP_JS.read_text(encoding="utf-8")
    start = src.index("function handleEvent")
    end = src.index("function start(", start)
    body = src[start:end]
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
