"""安全強化測試（#196 #198 #203 #205 #207）。

- #203 登入速率限制：連續失敗達上限即鎖定，期間連正確密碼也擋（防暴力破解）。
- #198 / #205 read_file 大小上限：超大檔拒讀回提示，不全量載入記憶體（防 OOM）。

純函式 + TestClient 整合；read_file 用 asyncio.run 跑 async execute，不依賴 pytest-asyncio。
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from studio import auth, config, tools, workspace


@pytest.fixture(autouse=True)
def _clear_login_fails():
    auth._LOGIN_FAILS.clear()
    yield
    auth._LOGIN_FAILS.clear()


# --- #203 登入速率限制 --------------------------------------------------
def test_not_locked_below_threshold():
    c = "9.9.9.9"
    for _ in range(auth.LOGIN_MAX_FAILS - 1):
        auth.register_login_result(c, False)
        assert auth.login_lock_remaining(c) == 0.0
    assert auth.login_lock_remaining(c) == 0.0


def test_locks_at_threshold():
    c = "9.9.9.9"
    for _ in range(auth.LOGIN_MAX_FAILS):
        auth.register_login_result(c, False)
    assert auth.login_lock_remaining(c) > 0


def test_success_clears_fail_count():
    c = "9.9.9.9"
    auth.register_login_result(c, False)
    auth.register_login_result(c, False)
    auth.register_login_result(c, True)
    assert auth.login_lock_remaining(c) == 0.0
    assert c not in auth._LOGIN_FAILS


def test_lock_expires(monkeypatch):
    c = "9.9.9.9"
    for _ in range(auth.LOGIN_MAX_FAILS):
        auth.register_login_result(c, False)
    assert auth.login_lock_remaining(c) > 0
    real = auth.time.time
    monkeypatch.setattr(auth.time, "time", lambda: real() + auth.LOGIN_LOCK_SECONDS + 1)
    assert auth.login_lock_remaining(c) == 0.0


def test_login_endpoint_429_after_lock(monkeypatch):
    from studio.server import app

    monkeypatch.setattr(config, "ACCESS_PASSWORD", "secret")
    client = TestClient(app)
    for _ in range(auth.LOGIN_MAX_FAILS):
        r = client.post("/api/login", json={"password": "wrong"})
        assert r.status_code == 401
    # 達上限後即使密碼正確也被鎖定擋下（429），不再進入密碼比對。
    r = client.post("/api/login", json={"password": "secret"})
    assert r.status_code == 429


# --- #198 / #205 read_file 大小上限 ------------------------------------
def test_workspace_read_file_rejects_oversize(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "WORKSPACE_ROOT", tmp_path)
    monkeypatch.setattr(config, "MAX_READ_FILE_BYTES", 100)
    root = workspace.create_workspace("sz")
    (root / "big.txt").write_text("x" * 500, encoding="utf-8")
    out = workspace.read_file("sz", "big.txt")
    assert out is not None and "過大" in out
    (root / "small.txt").write_text("ok", encoding="utf-8")
    assert workspace.read_file("sz", "small.txt") == "ok"


def test_tools_read_file_rejects_oversize(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MAX_READ_FILE_BYTES", 100)
    (tmp_path / "big.txt").write_text("y" * 500, encoding="utf-8")
    out = asyncio.run(tools.execute("read_file", {"path": "big.txt"}, tmp_path))
    assert "過大" in out
    (tmp_path / "s.txt").write_text("hi", encoding="utf-8")
    out2 = asyncio.run(tools.execute("read_file", {"path": "s.txt"}, tmp_path))
    assert out2 == "hi"
