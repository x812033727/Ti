"""任務 #5：「真實啟動 server」冒煙的 pytest 入口（讓 CI 跑得到，不留死碼）。

subprocess 以 TI_OFFLINE=1、TI_DISCUSS_MODE=round_robin、TI_PARALLEL_TASKS=0 真實啟動
`python -m studio.server`（真 uvicorn＋TCP），再以子行程執行 QA 的
tests/server/smoke_agenda_real_server.py 斷言 exit 0（議程拆解→分派硬驗證→逐子題討論
→彙整→history 重播，逐項 PASS/FAIL 見其輸出）。

跳過條件（環境因素，不算紅）：
- websockets / httpx 未安裝；
- 沙箱禁跨進程 loopback——server 進程活著且 log 顯示已啟動、但 TCP 連不上時 skip，
  server 自己掛掉則照樣 FAIL。
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
SCRIPT = ROOT / "tests" / "server" / "smoke_agenda_real_server.py"


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


def test_real_server_agenda_smoke(tmp_path):
    port = _free_port()
    env = os.environ.copy()
    env.update(
        {
            # 宿主可能已設門禁/並行等部署值，逐一明確覆蓋（與 smoke_agenda_run.sh 同款）。
            "TI_ACCESS_PASSWORD": "",
            "TI_OFFLINE": "1",
            "TI_OFFLINE_DELAY": "0",
            "TI_DISCUSS_MODE": "round_robin",
            "TI_AGENDA_ROUNDS": "1",
            "TI_DEBATE_ROUNDS": "1",
            "TI_PARALLEL_TASKS": "0",
            "TI_HUDDLE": "0",
            "TI_REFLEXION": "0",
            "TI_SELF_REFINE_ITERS": "0",
            "TI_OBJECTIVE_GATE": "0",
            # 宿主部署 env 可能設 TI_ADR=1：會多一次 senior 蒸餾發言＋事件，破壞
            # 冒煙的確定性斷言（發言數/事件數）。預設即 0，這裡明確釘死。
            "TI_ADR": "0",
            # 宿主可能設 TI_PUBLISH_AUTO=1＋GITHUB_TOKEN：done 後會多跑「發佈」階段
            # （事件數不齊），更嚴重的是把冒煙假專案推上真 repo——必須釘死關閉。
            "TI_PUBLISH_AUTO": "0",
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
            "真實 server 冒煙",
            log.read_text(encoding="utf-8", errors="replace")[-2000:],
        )
        assert "SMOKE PASS" in client.stdout
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
