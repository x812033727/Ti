"""systemd watchdog 對接(穩定強化 γ):零依賴 sd_notify + 心跳 notifier。

最外層兜底:β 的 loop monitor 只告警不自殺;真正的自救交 systemd——unit 換
Type=notify+WatchdogSec=300,行程每 60s 送 WATCHDOG=1,連續漏 5 次即被 systemd
重啟(Restart=always)。

守護不變量:
- _sd_notify:NOTIFY_SOCKET 不存在(非 systemd 環境/測試)→ 靜默 no-op 不拋;
  存在 → unix datagram 送達;socket 壞掉也不拋(通知失敗不得影響主迴圈)。
- notifier:READY=1 只在 main 起點送(Type=notify 啟動握手);WATCHDOG=1 每輪送,
  **暫停中也送**(paused 是活著,不該被 systemd 誤殺)。
"""

from __future__ import annotations

import asyncio
import socket

import pytest

from studio import autopilot, config


@pytest.fixture()
def notify_sock(tmp_path, monkeypatch):
    """真實 unix datagram socket 當假 systemd 收端。"""
    path = str(tmp_path / "notify.sock")
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    srv.bind(path)
    srv.settimeout(1)
    monkeypatch.setenv("NOTIFY_SOCKET", path)
    yield srv
    srv.close()


def test_sd_notify_noop_without_socket(monkeypatch):
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
    autopilot._sd_notify("READY=1")  # 不拋即通過


def test_sd_notify_sends_datagram(notify_sock):
    autopilot._sd_notify("READY=1")
    assert notify_sock.recv(64) == b"READY=1"


def test_sd_notify_swallows_socket_errors(monkeypatch, tmp_path):
    monkeypatch.setenv("NOTIFY_SOCKET", str(tmp_path / "gone.sock"))  # 無人監聽
    autopilot._sd_notify("WATCHDOG=1")  # ConnectionRefused 也不得冒泡


@pytest.mark.asyncio
async def test_watchdog_notifier_pings_even_while_paused(monkeypatch, notify_sock):
    monkeypatch.setattr(config, "autopilot_paused", lambda: True)
    ticks = {"n": 0}

    async def fast_sleep(_s):
        ticks["n"] += 1
        if ticks["n"] > 2:
            raise asyncio.CancelledError()

    monkeypatch.setattr(autopilot.asyncio, "sleep", fast_sleep)
    with pytest.raises(asyncio.CancelledError):
        await autopilot._watchdog_notifier()
    got = [notify_sock.recv(64) for _ in range(2)]
    assert got == [b"WATCHDOG=1", b"WATCHDOG=1"], "暫停中也要 ping(paused 是活著)"
