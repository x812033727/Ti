#!/usr/bin/env python3
"""Capture the metrics panel through raw W3C WebDriver HTTP calls.

This script intentionally avoids selenium/playwright. It seeds one deterministic
history session, starts the local FastAPI server, asks chromedriver to open the
metrics drawer, asserts visible DOM text, and writes a PNG screenshot.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = PROJECT_ROOT / "docs" / "screenshots" / "metrics_panel.png"
DEFAULT_HISTORY_ROOT = PROJECT_ROOT / "tmp" / "metrics_capture" / "history"
DEFAULT_SERVER_PORT = 8799


class CaptureError(RuntimeError):
    pass


@dataclass
class AssertionResult:
    name: str
    passed: bool
    detail: str


def _json_request(method: str, url: str, payload: dict[str, Any] | None = None) -> Any:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310 - local webdriver
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise CaptureError(f"{method} {url} failed: HTTP {exc.code} {body}") from exc
    except urllib.error.URLError as exc:
        raise CaptureError(f"{method} {url} failed: {exc}") from exc
    if not raw:
        return None
    return json.loads(raw.decode("utf-8"))


def _http_get_json(url: str, timeout_s: float = 2.0) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=timeout_s) as resp:  # noqa: S310 - local server
        return json.loads(resp.read().decode("utf-8"))


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_ready(base_url: str, timeout_s: float = 15.0) -> None:
    deadline = time.time() + timeout_s
    last_error = ""
    while time.time() < deadline:
        try:
            body = _http_get_json(f"{base_url}/api/health")
            if body.get("ok") is True:
                print(f"PASS health: {base_url}/api/health")
                return
            last_error = f"unexpected body: {body}"
        except Exception as exc:  # noqa: BLE001 - poll should keep retrying
            last_error = str(exc)
        time.sleep(0.5)
    raise CaptureError(f"server did not become ready in {timeout_s}s: {last_error}")


def _port_is_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _wait_port_closed(port: int, timeout_s: float = 5.0) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if not _port_is_open(port):
            return True
        time.sleep(0.2)
    return not _port_is_open(port)


def _seed_history(history_root: Path) -> None:
    os.environ["TI_HISTORY_ROOT"] = str(history_root)
    os.environ.setdefault("TI_HISTORY_MAX_COUNT", "0")

    from studio import history

    history_root.mkdir(parents=True, exist_ok=True)
    for old in history_root.glob("metrics-panel-fixture-*"):
        if old.is_file():
            old.unlink()
    sid = f"metrics-panel-fixture-{int(time.time())}"
    history.start_session(sid, "metrics panel screenshot fixture")
    events = [
        {"type": "task_status", "payload": {"id": 1, "title": "截圖驗證", "status": "doing"}},
        {"type": "task_status", "payload": {"id": 1, "title": "截圖驗證", "status": "review"}},
        {"type": "run_result", "payload": {"passed": True, "detail": "驗證通過"}},
        {"type": "task_status", "payload": {"id": 1, "title": "截圖驗證", "status": "done"}},
        {"type": "demo_result", "payload": {"passed": True, "output": "metrics panel rendered"}},
        {"type": "done", "payload": {"completed": True, "stopped": False}},
    ]
    for event in events:
        history.record_event(sid, event)
    meta = history.finish_session(sid)
    scorecard = (meta or {}).get("scorecard") or {}
    if not isinstance(scorecard, dict) or scorecard.get("tasks_total", 0) <= 0:
        raise CaptureError("history seed failed: scorecard was not derived")
    print(f"PASS seed: {sid} scorecard tasks_total={scorecard['tasks_total']}")


def _start_server(port: int, history_root: Path) -> subprocess.Popen:
    env = os.environ.copy()
    env.update(
        {
            "TI_HOST": "127.0.0.1",
            "TI_PORT": str(port),
            "TI_OFFLINE": "1",
            "TI_ACCESS_PASSWORD": "",
            "TI_HISTORY_ROOT": str(history_root),
            "TI_HISTORY_MAX_COUNT": "0",
        }
    )
    return subprocess.Popen(  # noqa: S603 - explicit local module command
        [sys.executable, "-m", "studio.server"],
        cwd=PROJECT_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )


def _start_chromedriver(port: int) -> subprocess.Popen:
    chromedriver = shutil.which("chromedriver")
    if not chromedriver:
        raise CaptureError("chromedriver not found on PATH")
    return subprocess.Popen(  # noqa: S603 - path resolved from PATH intentionally
        [chromedriver, f"--port={port}", "--url-base=/"],
        cwd=PROJECT_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )


def _terminate(proc: subprocess.Popen | None, name: str) -> None:
    if proc is None or proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=5)
    print(f"PASS cleanup: stopped {name}")


def _drain_output(proc: subprocess.Popen | None) -> str:
    if proc is None or proc.stdout is None:
        return ""
    try:
        return proc.stdout.read() or ""
    except Exception:  # noqa: BLE001 - diagnostic only
        return ""


class WebDriver:
    def __init__(self, base_url: str, chrome_binary: str | None):
        self.base_url = base_url.rstrip("/")
        self.chrome_binary = chrome_binary
        self.session_id: str | None = None

    def start(self) -> None:
        chrome_options: dict[str, Any] = {
            "args": [
                "--headless=new",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--window-size=1280,900",
            ]
        }
        if self.chrome_binary:
            chrome_options["binary"] = self.chrome_binary
        payload = {
            "capabilities": {
                "alwaysMatch": {
                    "browserName": "chrome",
                    "goog:chromeOptions": chrome_options,
                    "goog:loggingPrefs": {"browser": "ALL"},
                }
            }
        }
        res = _json_request("POST", f"{self.base_url}/session", payload)
        value = res.get("value", res)
        self.session_id = value.get("sessionId") or res.get("sessionId")
        if not self.session_id:
            raise CaptureError(f"webdriver session missing sessionId: {res}")
        print(f"PASS webdriver: session {self.session_id}")

    def _url(self, path: str) -> str:
        if not self.session_id:
            raise CaptureError("webdriver session not started")
        return f"{self.base_url}/session/{self.session_id}{path}"

    def navigate(self, url: str) -> None:
        _json_request("POST", self._url("/url"), {"url": url})

    def execute(self, script: str) -> Any:
        res = _json_request("POST", self._url("/execute/sync"), {"script": script, "args": []})
        return res.get("value")

    def screenshot(self) -> bytes:
        res = _json_request("GET", self._url("/screenshot"))
        return base64.b64decode(res["value"])

    def browser_logs(self) -> tuple[str, list[dict[str, Any]]]:
        for endpoint in ("/se/log", "/log"):
            try:
                res = _json_request("POST", self._url(endpoint), {"type": "browser"})
            except CaptureError as exc:
                if "HTTP 404" in str(exc) or "unknown command" in str(exc):
                    continue
                return (f"unavailable: {exc}", [])
            value = res.get("value")
            if isinstance(value, list):
                return ("available", value)
        return ("unavailable: browser log endpoint not supported", [])

    def close(self) -> None:
        if not self.session_id:
            return
        try:
            _json_request("DELETE", self._url(""), {})
            print("PASS cleanup: deleted webdriver session")
        except CaptureError as exc:
            print(f"WARN cleanup: webdriver delete failed: {exc}")
        finally:
            self.session_id = None


def _assert_metrics(driver: WebDriver, skip_open_panel: bool) -> list[AssertionResult]:
    if not skip_open_panel:
        driver.execute("return window.openMetrics && window.openMetrics();")
    time.sleep(1.0)
    state = driver.execute(
        """
        const panel = document.querySelector("#metricsPanel");
        const body = document.querySelector("#metricsBody");
        return {
          hasPanel: !!panel,
          panelHidden: panel ? panel.classList.contains("hidden") : null,
          text: body ? body.innerText : "",
        };
        """
    )
    text = (state or {}).get("text") or ""
    results = [
        AssertionResult(
            "#metricsPanel visible",
            bool(state and state.get("hasPanel") and not state.get("panelHidden")),
            f"hidden={None if not state else state.get('panelHidden')}",
        ),
        AssertionResult(
            '#metricsBody contains "活躍場次"',
            "活躍場次" in text,
            "found" if "活躍場次" in text else f"text={text[:120]!r}",
        ),
        AssertionResult(
            '#metricsBody contains "記分卡"',
            "記分卡" in text,
            "found" if "記分卡" in text else f"text={text[:120]!r}",
        ),
    ]
    for result in results:
        label = "PASS" if result.passed else "FAIL"
        print(f"{label} assert: {result.name} ({result.detail})")
    return results


def _write_png(path: Path, data: bytes, overwrite: bool) -> None:
    if path.exists() and not overwrite:
        existing = path.read_bytes()
        if not existing.startswith(b"\x89PNG\r\n\x1a\n") or not existing:
            raise CaptureError(f"{path} already exists but is not a valid PNG")
        print(
            f"WARN screenshot: {path.relative_to(PROJECT_ROOT)} exists; pass --overwrite to replace"
        )
        return
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise CaptureError("screenshot is not a PNG")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    print(f"PASS screenshot: wrote {path.relative_to(PROJECT_ROOT)} ({len(data)} bytes)")


def _looks_like_js_error(row: dict[str, Any]) -> bool:
    message = str(row.get("message", ""))
    return any(
        marker in message
        for marker in (
            "Uncaught",
            "TypeError",
            "ReferenceError",
            "SyntaxError",
            "EvalError",
            "RangeError",
            "URIError",
        )
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Capture the Ti Studio metrics panel PNG.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--history-root", type=Path, default=DEFAULT_HISTORY_ROOT)
    parser.add_argument("--server-port", type=int, default=DEFAULT_SERVER_PORT)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--skip-open-panel", action="store_true")
    parser.add_argument(
        "--chrome-binary",
        default=None,
        help="Optional real Chrome binary path. By default chromedriver picks its paired browser.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    server: subprocess.Popen | None = None
    chromedriver: subprocess.Popen | None = None
    driver: WebDriver | None = None
    chrome_port = _find_free_port()
    server_base = f"http://127.0.0.1:{args.server_port}"
    try:
        if _port_is_open(args.server_port):
            raise CaptureError(f"server port {args.server_port} is already in use")
        _seed_history(args.history_root)
        server = _start_server(args.server_port, args.history_root)
        _wait_ready(server_base)
        chromedriver = _start_chromedriver(chrome_port)
        time.sleep(1.0)
        driver = WebDriver(f"http://127.0.0.1:{chrome_port}", args.chrome_binary)
        driver.start()
        driver.navigate(f"{server_base}/")
        results = _assert_metrics(driver, args.skip_open_panel)
        failed = [r for r in results if not r.passed]
        log_status, logs = driver.browser_logs()
        severe = [row for row in logs if str(row.get("level", "")).upper() == "SEVERE"]
        js_errors = [row for row in severe if _looks_like_js_error(row)]
        if severe:
            print(f"WARN browser-log: {len(severe)} SEVERE entries")
            for row in severe[:5]:
                print(f"WARN browser-log-entry: {row.get('message', row)}")
        else:
            print(f"PASS browser-log: {log_status}, severe=0")
        if js_errors:
            for row in js_errors[:5]:
                print(f"FAIL js-error: {row.get('message', row)}")
            raise CaptureError("browser log contains JavaScript errors")
        print("PASS js-error: no JavaScript exception in browser log")
        if failed:
            raise CaptureError("DOM assertions failed")
        _write_png(args.output, driver.screenshot(), args.overwrite)
        return 0
    except CaptureError as exc:
        print(f"FAIL capture_metrics: {exc}", file=sys.stderr)
        return 1
    finally:
        if driver:
            driver.close()
        _terminate(chromedriver, "chromedriver")
        _terminate(server, "server")
        if not _wait_port_closed(args.server_port):
            print(f"WARN cleanup: port {args.server_port} still appears open", file=sys.stderr)
        server_log = _drain_output(server)
        chrome_log = _drain_output(chromedriver)
        if server and server.returncode not in (0, None, -signal.SIGTERM):
            print(f"WARN server log:\n{server_log[-2000:]}", file=sys.stderr)
        if chromedriver and chromedriver.returncode not in (0, None, -signal.SIGTERM):
            print(f"WARN chromedriver log:\n{chrome_log[-2000:]}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
