"""任務 #2 驗證：設定頁 token／API key 欄位渲染正確。

對齊驗收標準 #2：設定頁能看到 token 類欄位（GITHUB_TOKEN、ANTHROPIC_API_KEY），
password 型態、不預填明文、未設定時 set=false。

兩層驗證：
  (後端) 真實啟動服務、GET /api/settings，檢查欄位 metadata（label/placeholder/
         kind=password/secret/未設定時 set=false）。
  (前端) 把後端真實回傳的 fields 餵進真實 web/app.js 的 renderSettings()（Node 執行、
         記錄式 DOM），驗證 input type=password、value 空、label/placeholder 正確。
跑前清掉 GITHUB_TOKEN/ANTHROPIC_API_KEY 以驗 set=false；備份/還原 .env。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request

import pytest
from _repo import REPO_ROOT

ROOT = REPO_ROOT
ENV = ROOT / ".env"
HOST = "127.0.0.1"
PORT = 8011  # 避開 8000，互不干擾
BASE = f"http://{HOST}:{PORT}"
SECRET_ENVS = {"ANTHROPIC_API_KEY", "MINIMAX_API_KEY", "GITHUB_TOKEN"}


def _get(path: str, timeout: float = 3.0):
    with urllib.request.urlopen(BASE + path, timeout=timeout) as resp:
        return resp.status, resp.read().decode("utf-8", "replace")


@pytest.fixture(scope="module")
def fields():
    backup = ENV.read_bytes() if ENV.exists() else None
    env = dict(os.environ)
    env.pop("TI_ACCESS_PASSWORD", None)  # 門禁停用 → /api/settings 直接放行
    for k in SECRET_ENVS:
        env.pop(k, None)  # 確保秘密欄位「未設定」→ set=false
    env["TI_HOST"] = HOST
    env["TI_PORT"] = str(PORT)

    # config.py 的 load_dotenv() 會在 server import 時把 .env 內的值補回
    # os.environ；光從子程序 env dict 移除秘密欄位並不夠——必須同步把這些 key
    # 從 .env 暫時拿掉，否則「未設定」前提失效（set 會變 True、門禁被重新啟用）。
    # finally 區段會還原原始 .env。
    if backup is not None:
        kept = [
            ln
            for ln in backup.decode("utf-8", "replace").splitlines(keepends=True)
            if ln.split("=", 1)[0].strip() not in (SECRET_ENVS | {"TI_ACCESS_PASSWORD"})
        ]
        ENV.write_text("".join(kept), encoding="utf-8")

    proc = subprocess.Popen(
        [sys.executable, "-m", "studio.server"],
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        deadline = time.time() + 30
        data = None
        while time.time() < deadline:
            if proc.poll() is not None:
                break
            try:
                status, body = _get("/api/settings")
                if status == 200:
                    data = json.loads(body)["fields"]
                    break
            except Exception:
                time.sleep(0.4)
        if data is None:
            out = proc.stdout.read() if proc.poll() is not None and proc.stdout else ""
            pytest.fail(f"服務未就緒或 /api/settings 不可達。輸出：\n{out}")
        yield data
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        if backup is not None:
            ENV.write_bytes(backup)


def _by_env(fields):
    return {f["env"]: f for f in fields}


def test_token_fields_present(fields):
    """驗收 #2：設定頁包含 GITHUB_TOKEN、ANTHROPIC_API_KEY 兩個 token 類欄位。"""
    m = _by_env(fields)
    assert "GITHUB_TOKEN" in m
    assert "ANTHROPIC_API_KEY" in m


def test_secret_fields_are_password_kind(fields):
    """token／API key 欄位為 password 型態且標記 secret。"""
    m = _by_env(fields)
    for env in ("GITHUB_TOKEN", "ANTHROPIC_API_KEY", "MINIMAX_API_KEY"):
        f = m[env]
        assert f["kind"] == "password", f"{env} kind 應為 password"
        assert f["secret"] is True, f"{env} 應標記 secret"


def test_secret_fields_no_plaintext(fields):
    """秘密欄位 value 一律為空字串，不回傳明文。"""
    for f in fields:
        if f["secret"]:
            assert f["value"] == "", f"{f['env']} 不該回傳明文，實為 {f['value']!r}"


def test_unset_fields_set_false(fields):
    """未設定的 token 欄位 set=false（本測試已清掉這些 env）。"""
    m = _by_env(fields)
    for env in ("GITHUB_TOKEN", "ANTHROPIC_API_KEY", "MINIMAX_API_KEY"):
        assert m[env]["set"] is False, f"{env} 未設定時 set 應為 False"


def test_label_and_placeholder_present(fields):
    """每個欄位都有 label；token 類欄位帶有意義的 placeholder（引導格式）。"""
    m = _by_env(fields)
    for f in fields:
        assert f["label"], f"{f['env']} 缺 label"
    assert m["GITHUB_TOKEN"]["placeholder"].startswith("ghp_")
    assert m["ANTHROPIC_API_KEY"]["placeholder"].startswith("sk-ant")


def test_frontend_renders_password_inputs(fields, tmp_path):
    """前端 renderSettings() 真實渲染：token 欄位 → input type=password、value 空、
    label/placeholder 正確（用 Node 執行真實 web/app.js）。"""
    node = shutil.which("node")
    if node is None:
        pytest.skip("環境無 node，略過前端渲染驗證")
    fpath = tmp_path / "fields.json"
    fpath.write_text(json.dumps(fields), encoding="utf-8")
    harness = ROOT / "tests" / "frontend_settings_render_test.mjs"
    result = subprocess.run(
        [node, str(harness), str(fpath)],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
    )
    out = (result.stdout + result.stderr).strip()
    assert result.returncode == 0, f"前端渲染驗證失敗：\n{out}"
    assert "FRONTEND_OK" in out, out


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
