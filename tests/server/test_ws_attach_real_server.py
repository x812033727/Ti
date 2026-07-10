"""WS 斷線重掛真實 server 冒煙的 pytest 入口（樣板同 test_smoke_agenda_real_server）。

subprocess 以 TI_OFFLINE=1 真實啟動 `python -m studio.server`（真 uvicorn＋TCP），再以
子行程執行 tests/server/smoke_ws_attach_real_server.py：ws1 開場→硬斷線→ws2 attach
重掛→插話→done→與 /api/history 全量對帳。detach（斷線背景續跑）只有真 server 測得到
——TestClient 每條 WS 連線的迴圈在連線關閉即拆除（見 smoke script docstring）。

跳過條件（環境因素，不算紅）：websockets/httpx 未安裝；沙箱禁跨進程 loopback。
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from _real_server_client import assert_smoke_client_ok

pytest.importorskip("websockets")
pytest.importorskip("httpx")

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "tests" / "server" / "smoke_ws_attach_real_server.py"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def test_ws_attach_real_server(tmp_path):
    port = _free_port()
    env = os.environ.copy()
    env.update(
        {
            # 宿主可能已設門禁/發佈等部署值，逐一明確覆蓋（同 smoke_agenda 樣板）。
            "TI_ACCESS_PASSWORD": "",
            "TI_OFFLINE": "1",
            # 每筆發言間 0.05s：讓 ws1 有充裕時間「討論進行中」斷線、ws2 重掛。
            "TI_OFFLINE_DELAY": "0.05",
            "TI_PARALLEL_TASKS": "0",
            "TI_HUDDLE": "0",
            "TI_REFLEXION": "0",
            "TI_SELF_REFINE_ITERS": "0",
            "TI_OBJECTIVE_GATE": "0",
            "TI_ADR": "0",
            "TI_PUBLISH_AUTO": "0",
            "TI_PUBLISH_REPO": "",
            "TI_PORT": str(port),
            "TI_WORKSPACE_ROOT": str(tmp_path / "ws"),
            "TI_HISTORY_ROOT": str(tmp_path / "hist"),
        }
    )
    log = tmp_path / "server.log"
    with log.open("w", encoding="utf-8") as logf:
        server = subprocess.Popen(
            [sys.executable, "-m", "studio.server"],
            cwd=ROOT,
            env=env,
            stdout=logf,
            stderr=subprocess.STDOUT,
        )
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not _port_open(port):
            if server.poll() is not None:
                pytest.fail(
                    f"server 啟動即退出（rc={server.returncode}）：\n{log.read_text()[-2000:]}"
                )
            time.sleep(0.5)
        if not _port_open(port):
            booted = "Uvicorn running" in log.read_text(encoding="utf-8", errors="replace")
            if server.poll() is None and booted:
                pytest.skip("沙箱禁跨進程 loopback（server 已啟動但 TCP 連不上）——環境因素")
            pytest.fail(f"server 30s 內未就緒：\n{log.read_text()[-2000:]}")

        client = subprocess.run(
            [sys.executable, str(SCRIPT), str(port)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=180,
        )
        assert_smoke_client_ok(
            client,
            "WS 重掛真實 server 冒煙",
            log.read_text(encoding="utf-8", errors="replace")[-2000:],
        )
        assert "SMOKE PASS" in client.stdout
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
