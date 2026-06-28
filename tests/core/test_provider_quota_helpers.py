"""studio.provider_quota 的衍生 helper 單元測試（合成快照、不打外網）。

涵蓋：constrained（未就緒/錯誤/用量達門檻）、least_constrained_ready（挑最寬鬆就緒者）、
summarize_for_pm（角色標注/用量/重置倒數/未就緒/bucket 式 antigravity）、_usage 兩種結構。
"""

from __future__ import annotations

from studio import provider_quota as pq


def _snap(providers, updated_at=1000.0):
    return {"ok": True, "updated_at": updated_at, "providers": providers}


def _win(used, reset=None):
    """window 式 rate_limits（claude/codex/minimax）。"""
    return {"five_hour": {"used_percentage": used, "reset_at": reset}, "error": None}


def test_usage_window_and_bucket():
    win = pq._usage({"ready": True, "rate_limits": _win(40, 2000)})
    assert win == {"ready": True, "error": None, "max_used": 40, "soonest_reset": 2000}
    bucket = pq._usage(
        {
            "ready": True,
            "rate_limits": {
                "buckets": [
                    {"used_percentage": 30, "reset_at": 5000},
                    {"used_percentage": 70, "reset_at": 3000},
                ],
                "error": None,
            },
        }
    )
    assert bucket["max_used"] == 70 and bucket["soonest_reset"] == 3000


def test_constrained():
    snap = _snap(
        [
            {"key": "claude", "ready": True, "rate_limits": _win(20)},
            {"key": "codex", "ready": True, "rate_limits": _win(95)},
            {"key": "minimax", "ready": False, "rate_limits": None},
            {"key": "antigravity", "ready": True, "rate_limits": {"error": "unauthorized"}},
        ]
    )
    assert pq.constrained(snap, "claude") is False
    assert pq.constrained(snap, "codex") is True  # 用量 95% ≥ 門檻
    assert pq.constrained(snap, "minimax") is True  # 未就緒
    assert pq.constrained(snap, "antigravity") is True  # 查詢錯誤
    assert pq.constrained(snap, "不存在") is True  # 找不到


def test_least_constrained_ready():
    snap = _snap(
        [
            {"key": "claude", "ready": True, "rate_limits": _win(60)},
            {"key": "codex", "ready": True, "rate_limits": _win(15)},
            {"key": "minimax", "ready": False, "rate_limits": None},
            {"key": "antigravity", "ready": True, "rate_limits": {"error": "token_missing"}},
        ]
    )
    assert pq.least_constrained_ready(snap) == "codex"  # 15% 最低、就緒、無 error
    # 全部不可用 → None
    none_snap = _snap([{"key": "claude", "ready": False, "rate_limits": None}])
    assert pq.least_constrained_ready(none_snap) is None
    # 全部 ready 但用量達受限門檻 → None
    exhausted_snap = _snap(
        [
            {"key": "claude", "ready": True, "rate_limits": _win(90)},
            {"key": "codex", "ready": True, "rate_limits": _win(95)},
        ]
    )
    assert pq.least_constrained_ready(exhausted_snap) is None


def test_summarize_for_pm():
    snap = _snap(
        [
            {"key": "claude", "ready": True, "rate_limits": _win(45, 1000 + 1800)},  # 30 分後
            {"key": "codex", "ready": True, "rate_limits": _win(92)},
            {"key": "minimax", "ready": False, "rate_limits": None},
            {"key": "antigravity", "ready": True, "rate_limits": {"error": "unauthorized"}},
        ]
    )
    out = pq.summarize_for_pm(snap, {"pm": "claude", "qa": "claude", "engineer": "codex"})
    assert "claude（pm、qa 用）" in out and "用量 45%" in out and "30 分後重置" in out
    assert "codex（engineer 用）" in out and "⚠️用量 92%" in out  # 受限標警示
    assert "minimax：未就緒/不可用" in out
    assert "antigravity：額度查詢異常（unauthorized）" in out


def test_summarize_empty_map():
    snap = _snap([{"key": "claude", "ready": True, "rate_limits": _win(10)}])
    out = pq.summarize_for_pm(snap)
    assert out.startswith("- claude：") and "用量 10%" in out
