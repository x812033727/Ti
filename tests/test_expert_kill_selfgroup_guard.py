"""kill-first 自殺防護(2026-07-19 事故回歸):SDK 子程序與本行程同 process group 時,
_best_effort_kill_subprocess 絕不可 killpg(=殺整組=autopilot 自殺,SIGKILL crashloop
restart 14 次),必須退回只殺該 pid;確認異組才可整組殺。

背景:runner.kill_process_group 的「自成 group」契約只對 runner 以 start_new_session=True
啟動的程序成立;claude_agent_sdk 內部子程序無此保證。
"""

from __future__ import annotations

import os
from types import SimpleNamespace

from studio import runner
from studio.experts import Expert


def _expert_with_proc(pid):
    killed = {"pid_kill": 0}

    class _Proc:
        def __init__(self):
            self.pid = pid

        def kill(self):
            killed["pid_kill"] += 1

    proc = _Proc()
    ex = object.__new__(Expert)  # 不跑 __init__(免 SDK/角色依賴),只測回收方法
    ex._client = SimpleNamespace(_transport=SimpleNamespace(_process=proc))
    return ex, killed


def test_same_group_falls_back_to_single_kill(monkeypatch):
    ex, killed = _expert_with_proc(pid=12345)
    grp_calls = []
    monkeypatch.setattr(runner, "kill_process_group", lambda p: grp_calls.append(p.pid))
    # 同組:getpgid(child) == getpgid(0)
    monkeypatch.setattr(os, "getpgid", lambda pid: 777)
    ex._best_effort_kill_subprocess()
    assert killed["pid_kill"] == 1, "同組必須只殺該 pid"
    assert grp_calls == [], "同組絕不可 killpg(autopilot 自殺)"


def test_different_group_uses_killpg(monkeypatch):
    ex, killed = _expert_with_proc(pid=12345)
    grp_calls = []
    monkeypatch.setattr(runner, "kill_process_group", lambda p: grp_calls.append(p.pid))
    monkeypatch.setattr(os, "getpgid", lambda pid: 999 if pid == 12345 else 111)
    ex._best_effort_kill_subprocess()
    assert grp_calls == [12345], "異組才走整組殺"
    assert killed["pid_kill"] == 0


def test_getpgid_failure_swallowed(monkeypatch):
    ex, killed = _expert_with_proc(pid=12345)

    def boom(pid):
        raise ProcessLookupError

    monkeypatch.setattr(os, "getpgid", boom)
    ex._best_effort_kill_subprocess()  # 不得拋(外層 best-effort 契約)
    assert killed["pid_kill"] == 0
