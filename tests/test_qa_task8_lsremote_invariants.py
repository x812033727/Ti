"""QA 驗收：任務 #8（總驗收標準 #2）「ls-remote 防呆不變式」強化專測。

與 test_qa_task2 互補：task2 驗證主路徑，本檔聚焦「驗收標準 #2 的不變式」更嚴格的釘死：
- ls-remote 指令 argv 精確等於 git <cred> ls-remote --heads origin <branch>。
- 遠端已存在同名分支時，回傳「嚴格」為 (False, str) 型別。
- 「不執行任何 push / 不做任何覆寫」：ls-remote 之後不得有任何寫入型 git 指令
  （push）與任何 gh pr 動作（create/merge）。
- 多種 ls-remote 輸出格式（多行、CRLF、含其他 ref）都判為存在並中止。
- 多個 branch（不同 task id）都帶正確 branch 名。

全程攔截 autopilot._run，不碰網路。
"""

from __future__ import annotations

import asyncio

import pytest

from studio import autopilot, config

# 對齊 autopilot 模組常數，用於精確 argv 比對
_GIT_CRED = ["-c", "credential.helper=!gh auth git-credential"]


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
        return (0, "")

    def calls_after(self, fragment: str) -> list[list[str]]:
        """回傳第一個含 fragment 的呼叫「之後」的所有呼叫。"""
        for i, c in enumerate(self.calls):
            if fragment in " ".join(c):
                return self.calls[i + 1 :]
        return []

    def first_with(self, fragment: str) -> list[str]:
        for c in self.calls:
            if fragment in " ".join(c):
                return c
        return []


@pytest.fixture(autouse=True)
def _forbid_real_subprocess(monkeypatch):
    async def _boom(*args, **kwargs):
        raise RuntimeError("test forbids spawning a real subprocess (no network)")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _boom)


@pytest.fixture(autouse=True)
def _safe_config(monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_DRYRUN", False)
    monkeypatch.setattr(config, "AUTOPILOT_FORCE_PUSH", False)
    monkeypatch.setattr(config, "AUTOPILOT_BRANCH", "main")


def _install(monkeypatch, overrides):
    spy = RunSpy(overrides)
    monkeypatch.setattr(autopilot, "_run", spy)
    return spy


_HAS_CHANGE = {"rev-list --count": (0, "1")}


# === ls-remote argv 精確比對 ==========================================


@pytest.mark.asyncio
async def test_lsremote_argv_is_exact(monkeypatch):
    """ls-remote 指令必須精確為 git <cred> ls-remote --heads origin <branch>。"""
    task = {"id": "123", "title": "t", "detail": ""}
    branch = "autopilot/task-123"
    spy = _install(monkeypatch, {**_HAS_CHANGE})  # 遠端不存在 → 放行
    await autopilot._commit_push_merge("/clone", task)

    argv = spy.first_with("ls-remote")
    expected = ["git", *_GIT_CRED, "ls-remote", "--heads", "origin", branch]
    assert argv == expected, f"ls-remote argv 不符：{argv}"


# === 遠端已存在 → 嚴格回傳 (False, str)，且之後零寫入 =================


@pytest.mark.asyncio
async def test_remote_exists_returns_false_tuple_and_no_writes(monkeypatch):
    task = {"id": "55", "title": "t", "detail": ""}
    branch = "autopilot/task-55"
    spy = _install(
        monkeypatch,
        {**_HAS_CHANGE, "ls-remote --heads": (0, f"sha1\trefs/heads/{branch}\n")},
    )
    result = await autopilot._commit_push_merge("/clone", task)

    # 嚴格型別：tuple、第一元素 is False、第二元素 str
    assert isinstance(result, tuple) and len(result) == 2
    ok, msg = result
    assert ok is False
    assert isinstance(msg, str) and msg

    # 不變式：ls-remote 之後不得有任何 push / gh pr 動作（不做任何覆寫）
    after = spy.calls_after("ls-remote")
    joined_after = [" ".join(c) for c in after]
    assert all("push" not in j for j in joined_after), f"中止後仍有 push：{joined_after}"
    assert all("pr" not in c for c in after for c in [c]) or not any(
        "pr create" in j or "pr merge" in j for j in joined_after
    ), f"中止後仍有 gh pr 動作：{joined_after}"
    # 全序列也不得出現 push（雙重保險）
    assert not any("push" in " ".join(c) for c in spy.calls)


# === 多種 ls-remote 輸出格式都判為存在並中止 ==========================


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "ls_out",
    [
        "sha1\trefs/heads/autopilot/task-77\n",  # 單行
        "sha1\trefs/heads/autopilot/task-77\r\n",  # CRLF
        "  sha1\trefs/heads/autopilot/task-77  \n",  # 前後空白
        "a\trefs/heads/autopilot/task-77\nb\trefs/heads/other\n",  # 多行
    ],
)
async def test_various_nonempty_outputs_abort(monkeypatch, ls_out):
    task = {"id": "77", "title": "t", "detail": ""}
    spy = _install(monkeypatch, {**_HAS_CHANGE, "ls-remote --heads": (0, ls_out)})
    ok, msg = await autopilot._commit_push_merge("/clone", task)
    assert ok is False
    assert not any("push" in " ".join(c) for c in spy.calls)
    assert "遠端已存在" in msg


# === 多 branch：ls-remote 帶各自正確 branch 名 ========================


@pytest.mark.asyncio
@pytest.mark.parametrize("task_id", ["1", "42", "abc123", "999"])
async def test_lsremote_uses_correct_branch(monkeypatch, task_id):
    task = {"id": task_id, "title": "t", "detail": ""}
    branch = f"autopilot/task-{task_id}"
    spy = _install(monkeypatch, {**_HAS_CHANGE})
    await autopilot._commit_push_merge("/clone", task)
    argv = spy.first_with("ls-remote")
    assert argv[-1] == branch, f"ls-remote 應檢查 {branch}，實際 {argv}"
    # 放行後的 push 也針對同一 branch
    push = spy.first_with("push")
    assert branch in push
