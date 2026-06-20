"""任務 #4 驗證：後端持久化——token 寫入專案根目錄 .env，且行程環境變數同步生效。

對齊驗收標準 #4：.env 內出現對應鍵值，且 os.environ 同步更新（config.reload() 後生效）。

雙重各驗：
  (檔案) POST 後 .env 實際出現 dotenv.set_key 寫入的鍵值。
  (行程) os.environ[key] 同步更新、config.reload() 後 config.* 值即時改變（無需重啟）。
真實服務層另以 /api/health 的 provider 欄位佐證「行程內 reload 確實生效」——
  POST 切換 TI_PROVIDER 後，未重啟服務即可從 health 看到 provider 改變。
跑前備份 .env、跑後還原；用明顯假 token。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request

import pytest
from _repo import REPO_ROOT

from studio import config, settings

ROOT = REPO_ROOT
ENV = ROOT / ".env"
HOST = "127.0.0.1"
PORT = 8013
BASE = f"http://{HOST}:{PORT}"
SECRET_ENVS = {"ANTHROPIC_API_KEY", "MINIMAX_API_KEY", "GITHUB_TOKEN"}


# ---------------------------------------------------------------------------
# (A) In-process：直接驗 .env 檔 + os.environ + config.reload，完全不碰真實 .env
# ---------------------------------------------------------------------------
@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    keys = [f.env for f in settings.FIELDS]
    saved = {k: os.environ.get(k) for k in keys}
    yield tmp_path
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    config.reload()


def test_secret_token_persisted_to_env_file(sandbox):
    """秘密 token 寫入 .env 檔（dotenv.set_key）。"""
    settings.update({"GITHUB_TOKEN": "ghp_TEST_persist_4"})
    env_text = (sandbox / ".env").read_text()
    assert "GITHUB_TOKEN" in env_text
    assert "ghp_TEST_persist_4" in env_text


def test_token_synced_to_os_environ(sandbox):
    """寫入後 os.environ 同步更新（同一行程立即可見）。"""
    settings.update({"GITHUB_TOKEN": "ghp_TEST_env_4", "MINIMAX_API_KEY": "sk-test-4"})
    assert os.environ["GITHUB_TOKEN"] == "ghp_TEST_env_4"
    assert os.environ["MINIMAX_API_KEY"] == "sk-test-4"


def test_config_reload_takes_effect(sandbox):
    """config.reload() 後可調值即時更新（無需重啟）——驗收 #4 的『生效』。"""
    settings.update({"TI_MODEL_FAST": "claude-haiku-4-5", "TI_PROVIDER": "minimax"})
    assert config.MODEL_FAST == "claude-haiku-4-5"
    assert config.PROVIDER == "minimax"
    # secret 類也同步進 config（reload 內 GITHUB_TOKEN/MINIMAX_API_KEY 一起更新）
    settings.update({"GITHUB_TOKEN": "ghp_TEST_cfg_4"})
    assert config.GITHUB_TOKEN == "ghp_TEST_cfg_4"


def test_env_file_and_environ_consistent(sandbox):
    """同一鍵在 .env 檔與 os.environ 內值一致（雙來源不分歧）。"""
    settings.update({"TI_PUBLISH_REPO": "octo/outputs"})
    env_text = (sandbox / ".env").read_text()
    assert "octo/outputs" in env_text
    assert os.environ["TI_PUBLISH_REPO"] == "octo/outputs"


# ---------------------------------------------------------------------------
# (B) 真實服務：POST 後 .env 檔出現鍵值 + 行程內 reload 透過 /api/health 佐證
# ---------------------------------------------------------------------------
def _get(path, timeout=3.0):
    with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
        return r.status, json.loads(r.read().decode())


def _post(path, body, timeout=3.0):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode(),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, json.loads(r.read().decode())


@pytest.fixture(scope="module")
def live():
    """真實啟動服務，回傳 (proc, env_path_bytes_backup)；teardown 還原 .env。"""
    backup = ENV.read_bytes() if ENV.exists() else None
    env = dict(os.environ)
    env["TI_ACCESS_PASSWORD"] = ""
    for k in SECRET_ENVS:
        env.pop(k, None)
    env["TI_PROVIDER"] = "claude"  # 啟動為 claude，稍後 POST 切 minimax 以觀察 reload
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
        ok = False
        while time.time() < deadline:
            if proc.poll() is not None:
                break
            try:
                if _get("/api/health")[0] == 200:
                    ok = True
                    break
            except Exception:
                time.sleep(0.4)
        if not ok:
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


def test_live_post_writes_env_file(live):
    """真實 POST 後，專案根目錄 .env 實際出現對應鍵值。"""
    _post("/api/settings", {"GITHUB_TOKEN": "ghp_TEST_live_4", "TI_PUBLISH_REPO": "live/repo"})
    text = ENV.read_text()
    assert "GITHUB_TOKEN" in text and "ghp_TEST_live_4" in text
    assert "TI_PUBLISH_REPO" in text and "live/repo" in text


def test_live_reload_effect_via_health(live):
    """行程內生效佐證：POST 切換 provider 後，未重啟即可從 /api/health 看到改變。"""
    before = _get("/api/health")[1]["provider"]
    assert before == "claude"
    _post("/api/settings", {"TI_PROVIDER": "minimax"})
    after = _get("/api/health")[1]["provider"]
    assert after == "minimax", f"config.reload() 應讓行程內 provider 變 minimax，實為 {after}"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
