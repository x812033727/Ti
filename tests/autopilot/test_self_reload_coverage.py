"""Autopilot self-reload 覆蓋範圍：_self_sig 必須涵蓋整個 studio 套件,而非少數檔。

回歸：#218 只改 orchestrator.py,但舊 _SELF_FILES 只盯 autopilot/config/deploy/backlog →
self-reload 不觸發,跑著的 autopilot 一直用舊 orchestration 邏輯、撐到硬 timeout。改為監看
整包 studio/*.py 後,任何被依賴模組的部署都會在任務之間觸發 os.execv 重載。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from studio import autopilot


def _studio_pyfiles() -> list[Path]:
    base = Path(autopilot.__file__).resolve().parent
    return list(base.glob("*.py"))


def test_self_sig_covers_core_orchestration_modules():
    """_self_sig 必須把 orchestrator/experts/flow/conclusion 等核心模組納入計算。"""
    names = {p.name for p in _studio_pyfiles()}
    assert {"orchestrator.py", "experts.py", "flow.py", "conclusion.py", "providers.py"} <= names


def test_self_sig_equals_whole_package_mtime_sum():
    """_self_sig 等於整包 studio/*.py 的 mtime 總和——即任一檔變動都會改變簽章。"""
    expected = sum(p.stat().st_mtime for p in _studio_pyfiles())
    assert autopilot._self_sig() == pytest.approx(expected)


def test_self_sig_changes_when_orchestrator_mtime_bumped(monkeypatch, tmp_path):
    """把 _self_sig 的掃描根指到臨時套件:碰 orchestrator.py 的 mtime → 簽章改變。"""
    pkg = tmp_path / "studio"
    pkg.mkdir()
    for name in ("autopilot.py", "config.py", "orchestrator.py", "experts.py"):
        (pkg / name).write_text("x\n", encoding="utf-8")

    # 讓 _self_sig 掃臨時套件（取代真實 studio 目錄）。
    import studio.autopilot as ap

    monkeypatch.setattr(ap, "__file__", str(pkg / "autopilot.py"))
    before = ap._self_sig()
    # 只動 orchestrator.py（舊 _SELF_FILES 不含它）的 mtime → 簽章必須變。
    import os

    st = (pkg / "orchestrator.py").stat()
    os.utime(pkg / "orchestrator.py", (st.st_atime + 100, st.st_mtime + 100))
    assert ap._self_sig() != before
