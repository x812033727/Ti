"""子行程活性心跳的純函式：/proc CPU 快照（_proc_descendant_cpu）＋ delta 壓欄（_workers_field）。

背景（issue #285）：長輪多專家討論的 inter-message 間隔（單一長工具呼叫/長 thinking/單則
超長串流）期間完全無事件產出，events 檔 mtime 凍結 30-90 分鐘，被外部監控誤判死鎖並 restart，
丟失數小時進度。修法把人工診斷用的「對 claude 子行程做兩次 /proc utime/stime 取樣」自動化，
心跳寫進 status.json 的 workers 欄位，讓監控能肯定判定「有 worker 燒 CPU＝非死鎖」。

本檔測純函式：以 tmp 造假 /proc（傳 proc_root=）驗證解析與子樹展開，免 monkeypatch os；
另有一則真 /proc + spin 子行程的端到端測試（Linux only）。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

from studio import autopilot


def _write_fake_proc(root, pid, ppid, utime, stime, *, comm="worker"):
    """在假 /proc 下造 <pid>/stat：欄位佈局與真 /proc 一致（第 2 欄 comm 以括號包住）。

    stat 格式：``pid (comm) state ppid pgrp ... utime stime ...``（1-indexed）。comm 之後
    第 3 欄起共需鋪到第 15 欄（stime），故 state(3) + ppid(4) 之後補足 pgrp..cstime 佔位到
    第 15 欄。以 rpartition(')') 之後的 token 位置回推：rest[1]=ppid、rest[11]=utime、rest[12]=stime。
    """
    d = root / str(pid)
    d.mkdir()
    # rest（')' 之後）：state, ppid, pgrp, session, tty_nr, tpgid, flags,
    #                   minflt, cminflt, majflt, cmajflt, utime, stime, ...
    rest = ["R", str(ppid), "0", "0", "0", "-1", "0", "0", "0", "0", "0", str(utime), str(stime)]
    (d / "stat").write_text(f"{pid} ({comm}) " + " ".join(rest) + "\n", encoding="utf-8")


# --- _proc_descendant_cpu（/proc 解析＋後裔子樹展開）---------------------------


def test_proc_cpu_snapshot_parses_descendants(tmp_path):
    """root + 子 + 孫 + 無關行程：只回後裔（不含 root）、ticks=utime+stime、排除無關 pid。"""
    proc = tmp_path / "proc"
    proc.mkdir()
    _write_fake_proc(proc, 100, 1, 0, 0)  # root（自身，不應出現在結果）
    _write_fake_proc(proc, 200, 100, 5, 3)  # 子（8 ticks）
    _write_fake_proc(proc, 300, 200, 10, 0)  # 孫（10 ticks）—— 驗多層展開
    _write_fake_proc(proc, 999, 1, 7, 7)  # 無關行程（ppid=1，非後裔）

    snap = autopilot._proc_descendant_cpu(100, proc_root=str(proc))
    assert snap == {200: 8, 300: 10}


def test_proc_cpu_snapshot_comm_with_spaces_and_parens(tmp_path):
    """comm 含空白與括號（如 '(claude (helper))'）：rpartition(')') 仍正確定位後續欄位。"""
    proc = tmp_path / "proc"
    proc.mkdir()
    _write_fake_proc(proc, 100, 1, 0, 0)
    _write_fake_proc(proc, 200, 100, 4, 6, comm="claude (helper)")
    snap = autopilot._proc_descendant_cpu(100, proc_root=str(proc))
    assert snap == {200: 10}


def test_proc_cpu_snapshot_missing_proc_returns_none(tmp_path):
    """proc_root 不存在（非 Linux / 無 /proc）→ None，不拋例外。"""
    assert autopilot._proc_descendant_cpu(1, proc_root=str(tmp_path / "nonexistent")) is None


def test_proc_cpu_snapshot_vanishing_pid_skipped(tmp_path):
    """某 pid 目錄無 stat（行程在 scandir 與 open 之間消失）→ 略過該 pid，其餘照回、不崩。"""
    proc = tmp_path / "proc"
    proc.mkdir()
    _write_fake_proc(proc, 100, 1, 0, 0)
    _write_fake_proc(proc, 200, 100, 5, 5)
    (proc / "300").mkdir()  # 有目錄、無 stat（模擬消失中的行程）
    snap = autopilot._proc_descendant_cpu(100, proc_root=str(proc))
    assert snap == {200: 10}


def test_proc_cpu_snapshot_empty_when_no_children(tmp_path):
    """root 無任何後裔 → {}（明確零 worker，與 None 語義不同）。"""
    proc = tmp_path / "proc"
    proc.mkdir()
    _write_fake_proc(proc, 100, 1, 0, 0)
    _write_fake_proc(proc, 999, 1, 3, 3)  # 無關行程
    assert autopilot._proc_descendant_cpu(100, proc_root=str(proc)) == {}


# --- _workers_field（兩快照 delta → status.json 欄位）--------------------------


def test_workers_field_first_tick_unknown():
    # prev is None（首 tick，無前次可比）→ count 有值、cpu_active None。
    assert autopilot._workers_field(None, {1: 10}) == {"count": 1, "cpu_active": None}


def test_workers_field_cpu_advanced_true():
    # 任一子行程 CPU tick 前進 → True。
    assert autopilot._workers_field({1: 10, 2: 5}, {1: 12, 2: 5}) == {
        "count": 2,
        "cpu_active": True,
    }


def test_workers_field_cpu_idle_false():
    # 皆存在但無前進 → False（存活但閒置）。
    assert autopilot._workers_field({1: 10}, {1: 10}) == {"count": 1, "cpu_active": False}


def test_workers_field_proc_unavailable_all_none():
    # cur is None（/proc 不可用）→ count 與 cpu_active 皆 None。
    assert autopilot._workers_field({1: 10}, None) == {"count": None, "cpu_active": None}


def test_workers_field_pid_recycled_reports_false():
    # worker 於兩 tick 間換 pid 重生（無共同 pid）→ 該窗記 False（良性；60s 窗內 pid 穩定）。
    assert autopilot._workers_field({1: 10}, {2: 999}) == {"count": 1, "cpu_active": False}


@pytest.mark.skipif(not os.path.isdir("/proc"), reason="需真 /proc（Linux）")
def test_real_spin_subprocess_detected_active():
    """真 /proc 端到端：spin 子行程持續燒 CPU，兩次快照 delta 前進 → cpu_active True。"""
    p = subprocess.Popen([sys.executable, "-c", "while True: pass"])
    try:
        s1 = autopilot._proc_descendant_cpu(os.getpid(), proc_root="/proc")
        time.sleep(0.3)
        s2 = autopilot._proc_descendant_cpu(os.getpid(), proc_root="/proc")
        assert s1 is not None and p.pid in s1, "spin 子行程應被列入後裔"
        assert autopilot._workers_field(s1, s2)["cpu_active"] is True
    finally:
        p.kill()
        p.wait()
