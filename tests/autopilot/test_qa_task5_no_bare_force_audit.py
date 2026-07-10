"""QA 驗收：任務 #5 「檢視 _commit_push_merge 確認無任何裸 git push -f / --force 路徑」。

雙重稽核：
 (1) 靜態／AST：解析 _commit_push_merge 函式體所有字串常數，斷言不含裸 "-f"
     或單獨 "--force"（僅允許 --force-with-lease / --force-if-includes）。
 (2) 動態：對 FORCE_PUSH ∈ {False, True} 各跑一次，蒐集所有實際 push argv，
     逐一斷言無裸 -f / 裸 --force。涵蓋兩條 push 路徑（含 force 與不含 force）。
全程攔截 _run，不碰網路。
"""

from __future__ import annotations

import ast
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
_SRC = (_ROOT / "studio" / "autopilot.py").read_text(encoding="utf-8")
_TASK = {"id": "5", "title": "t", "detail": "d"}
_BRANCH = "autopilot/task-5"

_ALLOWED_FORCE = {"--force-with-lease", "--force-if-includes"}


def _func_string_constants(src: str, func_name: str) -> list[str]:
    """回傳指定函式體內所有字串字面值。"""
    tree = ast.parse(src)
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name:
            for sub in ast.walk(node):
                if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                    out.append(sub.value)
    return out


# ---- (1) AST 靜態稽核 ------------------------------------------------------


def test_ast_no_bare_force_literal_in_function():
    consts = _func_string_constants(_SRC, "_commit_push_merge")
    assert consts, "未解析到 _commit_push_merge 函式體字串"
    bad = [c for c in consts if c in ("-f", "--force") or c.startswith("--force=")]
    assert not bad, f"_commit_push_merge 出現裸 force 字面值：{bad}"
    # 若有 force token，必屬白名單
    force_tokens = [c for c in consts if c.startswith("--force")]
    assert all(t in _ALLOWED_FORCE for t in force_tokens), f"非白名單 force token：{force_tokens}"


def test_ast_function_exists_and_isolated():
    # 確認確實在分析目標函式（避免函式改名造成假綠）
    tree = ast.parse(_SRC)
    names = {
        n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert "_commit_push_merge" in names


# ---- (2) 動態：蒐集所有 push argv -----------------------------------------


class RunSpy:
    def __init__(self, overrides=None):
        self.overrides = overrides or {}
        self.calls: list[list[str]] = []

    async def __call__(self, cmd, cwd=None, timeout=600, **kwargs):
        self.calls.append(list(cmd))
        joined = " ".join(cmd)
        for key, val in self.overrides.items():
            if key in joined:
                return val
        if "remote get-url --push origin" in joined:
            return (0, f"https://github.com/{config.AUTOPILOT_REPO}.git")
        return (0, "")

    @property
    def push_argvs(self):
        return [c for c in self.calls if "push" in c]


@pytest.fixture(autouse=True)
def _no_subprocess(monkeypatch):
    async def _boom(*a, **k):
        raise RuntimeError("禁止真實子行程 / 網路")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _boom)


@pytest.fixture(autouse=True)
def _base_cfg(monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_DRYRUN", False)
    monkeypatch.setattr(config, "AUTOPILOT_REPO", "owner/repo")
    # owner allowlist 護欄：放行本檔測試用的 owner
    monkeypatch.setattr(config, "PUBLISH_OWNER_ALLOWLIST", frozenset({"owner"}))


def _assert_no_bare_force(argv):
    assert "-f" not in argv, f"出現裸 -f：{argv}"
    for tok in argv:
        if tok.startswith("--force"):
            assert tok in _ALLOWED_FORCE, f"非白名單 force token：{tok}"


def test_dynamic_default_push_no_force(monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_FORCE_PUSH", False)
    spy = RunSpy({"rev-list": (0, "1"), "ls-remote": (0, ""), "pr view": (0, "7")})
    monkeypatch.setattr(autopilot, "_run", spy)
    ok, msg = asyncio.run(autopilot._commit_push_merge("/clone", _TASK))
    assert ok, msg
    assert spy.push_argvs
    for argv in spy.push_argvs:
        _assert_no_bare_force(argv)
        assert not any(t.startswith("--force") for t in argv), "預設路徑不應有任何 force token"


def test_dynamic_force_push_only_lease_pair(monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_FORCE_PUSH", True)
    spy = RunSpy(
        {"rev-list": (0, "1"), "ls-remote": (0, "x\trefs/heads/" + _BRANCH), "pr view": (0, "7")}
    )
    monkeypatch.setattr(autopilot, "_run", spy)
    ok, msg = asyncio.run(autopilot._commit_push_merge("/clone", _TASK))
    assert ok, msg
    assert spy.push_argvs
    for argv in spy.push_argvs:
        _assert_no_bare_force(argv)
        force_tokens = [t for t in argv if t.startswith("--force")]
        assert force_tokens == ["--force-with-lease", "--force-if-includes"], force_tokens
