"""QA 驗收：任務「fail-safe 中止——不確定狀態一律中止，絕不 fall-through 當無保護」。

對應驗收標準 #3：不確定狀態（403／網路／逾時／其他未預期錯誤）一律判 unknown，
接點層中止並回含「無法確認保護狀態」字樣的訊息；測試證明絕不誤判為「無保護」而放行。

核心反例：只要「合併目標的 Rulesets 狀態未被乾淨確認」，就不得僅憑舊 protection 端點
的 404 斷定「無保護」——Rulesets 查詢失敗（5xx／未預期錯誤）屬不確定，必須 unknown。

手法：攔截 autopilot._run，依指令關鍵片段回傳 gh (rc, out)。函式層與接點層皆覆蓋。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from studio import autopilot, config

_REPO = "octo/Ti"
_MAIN = "main"
_TASK = {"id": "3", "title": "fail-safe 驗證", "detail": ""}


class RunSpy:
    def __init__(self, overrides=None):
        self.overrides = overrides or {}
        self.calls: list[list[str]] = []

    async def __call__(self, cmd, cwd=None, timeout=600):
        self.calls.append(list(cmd))
        joined = " ".join(cmd)
        for key, val in self.overrides.items():
            if key in joined:
                return val
        if "remote get-url --push origin" in joined:
            return (0, f"https://github.com/{config.AUTOPILOT_REPO}.git")
        return (0, "")

    def called(self, fragment: str) -> bool:
        return any(fragment in " ".join(c) for c in self.calls)


@pytest.fixture(autouse=True)
def _forbid_real_subprocess(monkeypatch):
    async def _boom(*args, **kwargs):
        raise RuntimeError("test forbids spawning a real subprocess (no network)")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _boom)


@pytest.fixture(autouse=True)
def _base_config(monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_REPO", _REPO)
    monkeypatch.setattr(config, "AUTOPILOT_BRANCH", _MAIN)
    monkeypatch.setattr(config, "AUTOPILOT_DRYRUN", False)
    monkeypatch.setattr(config, "AUTOPILOT_FORCE_PUSH", False)
    monkeypatch.setattr(config, "AUTOPILOT_PROTECTION_CHECK", True)


async def _state(monkeypatch, overrides):
    spy = RunSpy(overrides)
    monkeypatch.setattr(autopilot, "_run", spy)
    return await autopilot._check_branch_protection("/clone", _MAIN)


# === 函式層 fail-safe：各種不確定來源 → unknown，絕不 unprotected ========

# (label, overrides) — 每一項都是「不確定」，期望 unknown
_UNCERTAIN_CASES = [
    ("主端點 403", {"rules/branches": (1, "gh: Resource not accessible (HTTP 403)")}),
    ("主端點逾時(rc=-1)", {"rules/branches": (-1, "(逾時 60s)")}),
    (
        "主端點 500 + 舊端點 403",
        {"rules/branches": (1, "HTTP 500"), "/protection": (1, "HTTP 403")},
    ),
    (
        "主端點空 + 舊端點 500",
        {"rules/branches": (0, "[]"), "/protection": (1, "gh: server error (HTTP 500)")},
    ),
    (
        "主端點壞JSON + 舊端點 403",
        {"rules/branches": (0, "garbage"), "/protection": (1, "HTTP 403")},
    ),
    (
        "主端點 404 + 舊端點逾時",
        {"rules/branches": (1, "HTTP 404"), "/protection": (-1, "(逾時 60s)")},
    ),
    # ↓↓ 關鍵反例：Rulesets 查詢失敗(5xx/未預期錯誤)＝狀態未知，不得僅憑舊端點 404 放行 ↓↓
    (
        "主端點 500（rulesets 未確認）+ 舊端點 404",
        {"rules/branches": (1, "gh: server error (HTTP 500)"), "/protection": (1, "HTTP 404")},
    ),
    (
        "主端點未預期錯誤（無 HTTP 碼）+ 舊端點 404",
        {"rules/branches": (1, "gh: connection reset"), "/protection": (1, "HTTP 404")},
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("label,overrides", _UNCERTAIN_CASES, ids=[c[0] for c in _UNCERTAIN_CASES])
async def test_uncertain_never_fallthrough_to_unprotected(monkeypatch, label, overrides):
    state, detail = await _state(monkeypatch, overrides)
    assert state != "unprotected", f"[{label}] 不確定狀態誤判為無保護（fall-through）：{detail}"
    assert state == "unknown", f"[{label}] 應判 unknown，實得 {state}：{detail}"


# === 函式層正向：唯有「明確無保護」才放行（不過度中止）===================


@pytest.mark.asyncio
async def test_clearly_unprotected_empty_and_404(monkeypatch):
    state, _ = await _state(
        monkeypatch, {"rules/branches": (0, "[]"), "/protection": (1, "HTTP 404")}
    )
    assert state == "unprotected"


@pytest.mark.asyncio
async def test_clearly_protected_via_rules(monkeypatch):
    state, _ = await _state(
        monkeypatch, {"rules/branches": (0, json.dumps([{"type": "pull_request"}]))}
    )
    assert state == "protected"


# === 接點層 fail-safe：unknown → 中止、不 push、訊息含明確字樣 ===========


@pytest.mark.asyncio
async def test_gate_aborts_on_uncertain_with_clear_message(monkeypatch):
    """以「主端點 500 + 舊端點 404」這個不確定組合走完整接點，必須中止。"""
    spy = RunSpy(
        {
            "rev-list --count": (0, "1"),
            "ls-remote --heads": (0, ""),
            "rules/branches": (1, "gh: server error (HTTP 500)"),
            "/protection": (1, "HTTP 404"),
        }
    )
    monkeypatch.setattr(autopilot, "_run", spy)
    ok, msg = await autopilot._commit_push_merge("/clone", _TASK)
    assert ok is False, "不確定狀態必須中止，不可放行 merge"
    assert "無法確認保護狀態" in msg, f"中止訊息須含明確字樣：{msg}"
    assert not spy.called("push") and not spy.called("pr merge")
