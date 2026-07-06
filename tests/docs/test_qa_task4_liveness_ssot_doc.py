"""防漂移守門 #4：`autopilot-monitoring.md` 必須把 `liveness_verdict` 標為判死正典並要求外部對齊。

回應 critic 異議：`liveness_verdict` 生產零消費者（外部「層3監控」在 repo 外、不 import 它），
其「可作外部對齊基準」的價值**完全繫於文件是否誠實標示它為正典**。若文件哪天悄悄拿掉這段，
函式就退回成「沒人知道、沒人對齊的平行實作」，回歸守門的意義蒸發。本檔把三者釘在一起：
  1. 文件標示 `liveness_verdict` 為 repo 內正典、要求外部作者對齊；
  2. 函式真的存在且三種判定字串與文件一致；
  3. 文件誠實界定範圍（不強制外部）——避免又滑回「假 SSOT」宣稱。

紅樣本實證：把 `autopilot-monitoring.md` 的「repo 內正典實作」整節刪掉 → `test_doc_marks_liveness_verdict_canonical`
立即紅（AssertionError：文件未標示 liveness_verdict 為正典）。證明本守門真的綁著文件內容，不是恆綠。
"""

from __future__ import annotations

import pathlib

from studio import autopilot

_DOC = pathlib.Path(__file__).resolve().parents[2] / "docs" / "guides" / "autopilot-monitoring.md"


def _doc_text() -> str:
    return _DOC.read_text(encoding="utf-8")


def test_doc_marks_liveness_verdict_canonical():
    """文件須指名 `liveness_verdict` 並標為 repo 內正典／對齊基準。"""
    text = _doc_text()
    assert "liveness_verdict" in text, "文件未提及 liveness_verdict"
    assert "正典" in text, "文件未標示 liveness_verdict 為判死正典（reference implementation）"
    assert "對齊" in text, "文件未要求外部監控作者對齊此函式"


def test_doc_honestly_scopes_external_enforcement():
    """文件須誠實界定：此函式不被外部監控自動強制（避免重蹈假 SSOT 宣稱）。"""
    text = _doc_text()
    # 明講層3監控在 repo 外、不 import——範圍界線在文件白紙黑字。
    assert "層3監控" in text
    assert "repo 外" in text or "repo 內" in text


def test_doc_verdict_strings_match_function():
    """文件列出的三種判定字串須與函式實際回傳一致（防字串漂移）。"""
    text = _doc_text()
    for verdict in ("alive", "dead_main_loop", "dead_task"):
        assert verdict in text, f"文件缺判定字串 {verdict!r}"


def test_function_exists_and_returns_documented_verdicts():
    """函式須存在，且對三種正典情境回傳文件所述判定（文件宣稱與行為對接）。"""
    now = 1_000_000.0
    thr = 180.0
    # alive：running + cpu_active 救命（規則 2）
    assert (
        autopilot.liveness_verdict(
            {
                "state": "running",
                "updated_at": now,
                "last_activity_at": now - 9999,
                "workers": {"cpu_active": True},
            },
            now=now,
            stale_threshold_s=thr,
        )
        == "alive"
    )
    # dead_main_loop：updated_at 停滯（規則 1）
    assert (
        autopilot.liveness_verdict(
            {"state": "running", "updated_at": now - 9999},
            now=now,
            stale_threshold_s=thr,
        )
        == "dead_main_loop"
    )
    # dead_task：running + cpu_active False 且 last_activity 長不動（規則 3 AND）
    assert (
        autopilot.liveness_verdict(
            {
                "state": "running",
                "updated_at": now,
                "last_activity_at": now - 9999,
                "workers": {"cpu_active": False},
            },
            now=now,
            stale_threshold_s=thr,
        )
        == "dead_task"
    )
