"""QA 驗收：任務 #7（總驗收標準 #1）「預設 push 不含 -f/--force，grep push -f 無結果」。

最終把關，雙層驗證：
- Source-level：掃描整個 repo 的原始碼（排除 tests 自身的檢查斷言），
  不得殘留 `push -f` 或 `"push", "-f"` 字面。
- Runtime：預設 config（AUTOPILOT_FORCE_PUSH=False、非 dryrun）下實際呼叫
  _commit_push_merge，攔截到的 push argv 不含 -f / --force / lease / if-includes。

手法：source 掃描走檔案讀取；runtime 走 _run 攔截，全程不碰網路。
"""

from __future__ import annotations

import asyncio

import pytest
from _repo import REPO_ROOT

from studio import autopilot, config, publisher


@pytest.fixture(autouse=True)
def _merge_flow_merged(monkeypatch):
    """Option 2 後合併走 publisher._merge_flow（等 CI→合併）。本檔聚焦 push/protection 旗標，
    一律把 _merge_flow 打成回 MERGED，讓 _commit_push_merge 能走完合併段、回 (True, ...)。"""

    async def _merged(number, payload, **kwargs):
        return (publisher.MergeOutcome.MERGED, "sha")

    monkeypatch.setattr(publisher, "_merge_flow", _merged)


_ROOT = REPO_ROOT
_TASK = {"id": "1", "title": "t", "detail": ""}
_BRANCH = "autopilot/task-1"

# 掃描的原始碼副檔名；排除測試（其檢查斷言本就含 "push -f" 字面）與快取。
_SCAN_EXTS = {".py", ".sh", ".md", ".service", ".toml"}


def _source_files():
    for p in _ROOT.rglob("*"):
        if p.suffix not in _SCAN_EXTS:
            continue
        parts = set(p.parts)
        if "tests" in parts or "__pycache__" in parts or ".venv" in parts:
            continue
        if "ti_studio.egg-info" in parts or ".git" in parts:
            continue
        # 排除 ephemeral 的工作區/autopilot 狀態（內含舊版 clone,非專案原始碼）
        if "workspaces" in parts or "autopilot" in parts:
            continue
        yield p


# === Source-level：全 repo 不得殘留裸 push -f ==========================


def test_no_bare_push_f_anywhere_in_source():
    offenders = []
    for p in _source_files():
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if "push -f" in text or '"push", "-f"' in text or "'push', '-f'" in text:
            offenders.append(str(p.relative_to(_ROOT)))
    assert not offenders, f"仍有裸 push -f 殘留於：{offenders}"


def test_autopilot_push_command_has_no_bare_force():
    """autopilot.py 的 push 指令字面不得含獨立的 '-f' 或裸 '--force' token。"""
    text = (_ROOT / "studio" / "autopilot.py").read_text(encoding="utf-8")
    assert '"-f"' not in text, 'autopilot.py 不應出現獨立 "-f" token'
    # 裸 "--force"（後面緊接結束引號）不該存在；--force-with-lease/-if-includes 是合法的不同 token
    assert '"--force"' not in text, 'autopilot.py 不應出現裸 "--force" token'


# === Runtime：預設 push argv 無任何 force 旗標 =========================


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

    @property
    def push_argv(self):
        for c in self.calls:
            if "push" in c:
                return c
        return []


@pytest.fixture(autouse=True)
def _forbid_real_subprocess(monkeypatch):
    async def _boom(*args, **kwargs):
        raise RuntimeError("test forbids spawning a real subprocess (no network)")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _boom)


@pytest.mark.asyncio
async def test_default_runtime_push_has_no_force_flags(monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_DRYRUN", False)
    monkeypatch.setattr(config, "AUTOPILOT_FORCE_PUSH", False)
    monkeypatch.setattr(config, "AUTOPILOT_BRANCH", "main")
    spy = RunSpy({"rev-list --count": (0, "1"), "pr view": (0, "7")})  # 遠端不存在、有變更
    monkeypatch.setattr(autopilot, "_run", spy)

    ok, msg = await autopilot._commit_push_merge("/clone", _TASK)
    argv = spy.push_argv
    assert argv, "未呼叫 push"
    for bad in ("-f", "--force", "--force-with-lease", "--force-if-includes"):
        assert bad not in argv, f"預設 push 不應含 {bad}：{argv}"
    # 形態正確
    assert argv.count("push") == 1
    assert "-u" in argv and "origin" in argv and _BRANCH in argv
    assert ok is True
