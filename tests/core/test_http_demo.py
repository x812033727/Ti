"""HTTP 驗收測試：parse_demo_url 安全過濾、run_http_demo 真實啟動/探測/收掉、
orchestrator 在宣告 `Demo 網址:` 時自測與最終 Demo 改走 HTTP 路徑。
"""

from __future__ import annotations

import asyncio
import socket

import pytest

from studio import config, events, runner
from studio.orchestrator import StudioSession
from studio.roles import BY_KEY, Role

# --- parse_demo_url ------------------------------------------------------


def test_parse_demo_url_loopback_only():
    assert (
        runner.parse_demo_url("執行指令: python app.py\nDemo 網址: http://localhost:8123/")
        == "http://localhost:8123/"
    )
    assert runner.parse_demo_url("Demo 網址: `http://127.0.0.1:5000/health`") == (
        "http://127.0.0.1:5000/health"
    )
    assert runner.parse_demo_url("Demo 網址: http://127.1.2.3:5000/health") == (
        "http://127.1.2.3:5000/health"
    )
    assert runner.parse_demo_url("Demo 網址: http://[::1]:5000/health") == (
        "http://[::1]:5000/health"
    )
    assert runner.parse_demo_url("Demo 網址: http://[::ffff:127.0.0.1]:5000/") == (
        "http://[::ffff:127.0.0.1]:5000/"
    )
    # 非本機一律拒絕（不對外部主機發請求）
    assert runner.parse_demo_url("Demo 網址: http://example.com/") is None
    assert runner.parse_demo_url("Demo 網址: http://10.0.0.1:5000/") is None
    assert runner.parse_demo_url("Demo 網址: http://[::ffff:192.0.2.1]:5000/") is None
    assert runner.parse_demo_url("Demo 網址: http://localhost.evil.com/") is None
    assert runner.parse_demo_url("Demo 網址: http://user@localhost:5000/") is None
    assert runner.parse_demo_url("Demo 網址: ftp://localhost:5000/") is None
    assert runner.parse_demo_url("沒有宣告") is None


# --- run_http_demo（真實啟動小型 HTTP server）-----------------------------


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def test_run_http_demo_probes_real_server(tmp_path):
    (tmp_path / "index.html").write_text("<h1>hello ti</h1>", encoding="utf-8")
    port = _free_port()
    cmd = f"python3 -m http.server {port} --bind 127.0.0.1"
    result, status = _run(
        runner.run_http_demo(tmp_path, cmd, f"http://127.0.0.1:{port}/", timeout=20, sandbox=False)
    )
    assert status == 200
    assert result.ok
    assert "HTTP 200" in result.output
    assert "hello ti" in result.output  # 真的拿到頁面內容


def test_run_http_demo_server_crash(tmp_path):
    port = _free_port()
    result, status = _run(
        runner.run_http_demo(
            tmp_path,
            "python3 -c 'import sys; sys.exit(3)'",
            f"http://127.0.0.1:{port}/",
            timeout=10,
            sandbox=False,
        )
    )
    assert status is None
    assert not result.ok
    assert "退出" in result.output and "exit=3" in result.output


def test_run_http_demo_never_ready(tmp_path):
    port = _free_port()
    result, status = _run(
        runner.run_http_demo(
            tmp_path,
            "sleep 60",  # 活著但永遠不開 port
            f"http://127.0.0.1:{port}/",
            timeout=2,
            sandbox=False,
        )
    )
    assert status is None
    assert not result.ok
    assert result.timed_out
    assert "未就緒" in result.output


# --- orchestrator 路由：宣告 Demo 網址 → 自測/Demo 走 HTTP 路徑 -------------


class StubExpert:
    def __init__(self, role: Role, scripts: list[str]):
        self.role = role
        self._scripts = scripts
        self.calls = 0
        self.prompts: list[str] = []

    async def speak(self, prompt: str, broadcast) -> str:
        self.prompts.append(prompt)
        text = self._scripts[min(self.calls, len(self._scripts) - 1)]
        self.calls += 1
        await broadcast(
            events.expert_message("t", self.role.key, self.role.name, self.role.avatar, text)
        )
        return text

    async def stop(self) -> None:
        pass


@pytest.fixture(autouse=True)
def _flow(monkeypatch):
    monkeypatch.setattr(config, "DEBATE_ROUNDS", 0)
    monkeypatch.setattr(config, "PARALLEL_TASKS_ENABLED", False)
    monkeypatch.setattr(config, "CLARIFY_ENABLED", False)


@pytest.mark.asyncio
async def test_session_uses_http_demo_when_url_declared(tmp_path, monkeypatch):
    http_calls: list[tuple[str, str]] = []

    async def fake_http_demo(cwd, command, url, timeout=None, sandbox=None):
        http_calls.append((command, url))
        return runner.RunOutput(f"{command} ⇒ GET {url}", 0, "GET → HTTP 200", False), 200

    plain_calls: list[str] = []
    real_run = runner.run_command

    async def spy_run_command(cwd, command, *a, **kw):
        plain_calls.append(command)
        return await real_run(cwd, command, *a, **kw)

    monkeypatch.setattr(runner, "run_http_demo", fake_http_demo)
    monkeypatch.setattr(runner, "run_command", spy_run_command)

    experts = {
        "pm": StubExpert(
            BY_KEY["pm"],
            [
                "任務: 做網站\n執行指令: python app.py\nDemo 網址: http://localhost:8123/",
                "決議: 完成",
                "檢討",
            ],
        ),
        "engineer": StubExpert(BY_KEY["engineer"], ["做好了\n執行指令: python app.py"]),
        "qa": StubExpert(BY_KEY["qa"], ["驗證: PASS"]),
        "senior": StubExpert(BY_KEY["senior"], ["決議: 核可"]),
    }
    bucket: list[events.StudioEvent] = []

    async def bc(ev):
        bucket.append(ev)

    session = StudioSession("t", bc, experts=experts, cwd=tmp_path)
    await session.run("做一個網站")

    # 自測與最終 Demo 都走 HTTP 路徑（兩次呼叫），demo 指令不再進純 run_command
    assert len(http_calls) == 2
    assert all(url == "http://localhost:8123/" for _, url in http_calls)
    assert not any("app.py" in c for c in plain_calls)
    # demo_result 事件標 HTTP Demo 且 passed
    demos = [e for e in bucket if e.type == events.EventType.DEMO_RESULT]
    assert demos and demos[-1].payload["label"] == "HTTP Demo"
    assert demos[-1].payload["passed"] is True


@pytest.mark.asyncio
async def test_engineer_declared_url_picked_up(tmp_path, monkeypatch):
    """PM 沒宣告、工程師在實作回報補宣告 → 自測即改走 HTTP。"""
    seen: list[str] = []

    async def fake_http_demo(cwd, command, url, timeout=None, sandbox=None):
        seen.append(url)
        return runner.RunOutput(f"{command} ⇒ GET {url}", 0, "HTTP 200", False), 200

    monkeypatch.setattr(runner, "run_http_demo", fake_http_demo)
    experts = {
        "pm": StubExpert(BY_KEY["pm"], ["任務: 做網站", "決議: 完成", "檢討"]),
        "engineer": StubExpert(
            BY_KEY["engineer"],
            ["做好了\n執行指令: python app.py\nDemo 網址: http://127.0.0.1:9000/health"],
        ),
        "qa": StubExpert(BY_KEY["qa"], ["驗證: PASS"]),
        "senior": StubExpert(BY_KEY["senior"], ["決議: 核可"]),
    }

    async def bc(ev):
        pass

    session = StudioSession("t", bc, experts=experts, cwd=tmp_path)
    await session.run("做一個網站")
    assert seen and all(u == "http://127.0.0.1:9000/health" for u in seen)
