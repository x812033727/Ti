"""任務 #3 驗證：前端填入測試用 token 並送出，POST /api/settings 回 {ok:true}。

對齊驗收標準 #3：前端填入 token 送出後，POST /api/settings 回傳 {ok:true}，UI 顯示成功。

兩層驗證：
  (後端) 真實啟動服務，對 /api/settings POST 假 token（ghp_TEST_xxx），驗證回 {ok:true}
         且回應帶回 fields；同步驗證壞 payload（非 dict）回 400。
  (前端) Node 執行真實 web/app.js：renderSettings → 在秘密欄位填測試 token → saveSettings()，
         驗證真的發出 POST、payload 正確（含填入值、略過留空秘密欄位）、UI 顯示成功。
跑前備份 .env，跑後還原；用明顯假 token，避免污染真實金鑰。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

import pytest
from _repo import REPO_ROOT

ROOT = REPO_ROOT
ENV = ROOT / ".env"
HOST = "127.0.0.1"
PORT = 8012
BASE = f"http://{HOST}:{PORT}"
SECRET_ENVS = {"ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GITHUB_TOKEN"}
TEST_TOKEN = "ghp_TEST_post_0003"


def _get(path: str, timeout: float = 3.0):
    with urllib.request.urlopen(BASE + path, timeout=timeout) as resp:
        return resp.status, resp.read().decode("utf-8", "replace")


def _post(path: str, body, timeout: float = 3.0):
    data = body if isinstance(body, (bytes, bytearray)) else json.dumps(body).encode()
    req = urllib.request.Request(
        BASE + path,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")


@pytest.fixture(scope="module")
def server():
    backup = ENV.read_bytes() if ENV.exists() else None
    env = dict(os.environ)
    env.pop("TI_ACCESS_PASSWORD", None)  # 門禁停用 → 首次設定路徑直接放行
    for k in SECRET_ENVS:
        env.pop(k, None)
    env["TI_HOST"] = HOST
    env["TI_PORT"] = str(PORT)
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
        ready = False
        while time.time() < deadline:
            if proc.poll() is not None:
                break
            try:
                if _get("/api/settings")[0] == 200:
                    ready = True
                    break
            except Exception:
                time.sleep(0.4)
        if not ready:
            out = proc.stdout.read() if proc.poll() is not None and proc.stdout else ""
            pytest.fail(f"服務未就緒。輸出：\n{out}")
        yield proc
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        if backup is not None:
            ENV.write_bytes(backup)


def test_post_token_returns_ok(server):
    """驗收 #3：POST 測試 token，回 {ok:true}。"""
    status, data = _post("/api/settings", {"GITHUB_TOKEN": TEST_TOKEN})
    assert status == 200
    assert data.get("ok") is True, data


def test_post_response_includes_fields(server):
    """回應同時帶回最新 fields，供前端 re-render；秘密欄位仍不回明文。"""
    status, data = _post("/api/settings", {"GITHUB_TOKEN": TEST_TOKEN})
    assert status == 200 and data["ok"] is True
    by_env = {f["env"]: f for f in data["fields"]}
    assert by_env["GITHUB_TOKEN"]["value"] == ""  # 不回明文
    assert by_env["GITHUB_TOKEN"]["set"] is True  # 已寫入 → set 翻 true


def test_post_non_dict_rejected(server):
    """壞 payload（非物件）回 400，不誤判為成功。"""
    status, data = _post("/api/settings", json.dumps([1, 2, 3]).encode())
    assert status == 400
    assert data.get("ok") is False


def test_frontend_fill_and_save(server, tmp_path):
    """前端真實流程：填 token → 按儲存 → 發出 POST、payload 正確、UI 顯示成功。"""
    node = shutil.which("node")
    if node is None:
        pytest.skip("環境無 node，略過前端送出驗證")
    # 取後端真實 fields 當作前端渲染輸入
    _, body = _get("/api/settings")
    fields = json.loads(body)["fields"]
    fpath = tmp_path / "fields.json"
    fpath.write_text(json.dumps(fields), encoding="utf-8")
    harness = ROOT / "tests" / "frontend_settings_save_test.mjs"
    result = subprocess.run(
        [node, str(harness), str(fpath)],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
    )
    out = (result.stdout + result.stderr).strip()
    assert result.returncode == 0, f"前端送出驗證失敗：\n{out}"
    assert "FRONTEND_SAVE_OK" in out, out


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
