"""任務 #5 驗證：重新讀取 GET /api/settings，秘密欄位不回顯明文、僅回報 set=true。

對齊驗收標準 #5：重新 GET /api/settings 時秘密欄位 value 為空、set=true；非秘密欄位正常回顯。

真實流程（手動走一次）：
  起服務 → POST 寫入秘密 token（ghp_TEST.../sk-ant-test）+ 非秘密值 → 重新 GET：
    - 秘密欄位 value=="" 且 set==True
    - 非秘密欄位 value 照常回顯
    - 整個 GET 回應字串不含任何明文 token（防洩漏紅線）
  前端：把已設定（set=true）的 fields 餵進真實 renderSettings()，驗證秘密欄位 input.value 仍空、
        placeholder 切為「已設定（留空＝不變更）」，非秘密欄位回顯實際值。
跑前備份 .env、跑後還原；用明顯假 token。
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
PORT = 8014
BASE = f"http://{HOST}:{PORT}"
SECRET_ENVS = {"ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GITHUB_TOKEN"}

GH = "ghp_TEST_reread_5"
ANT = "sk-ant-TEST_reread_5"
LEAD = "claude-haiku-4-5"  # 須為 CLAUDE_MODELS 白名單內的合法值（select 後端擋非法）
REPO = "octo/outputs-5"


def _get(path, timeout=3.0):
    with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
        return r.status, r.read().decode()


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
def reread():
    """起服務 → POST 寫入秘密+非秘密 → 回傳 (raw_get_body, fields)。teardown 還原 .env。"""
    backup = ENV.read_bytes() if ENV.exists() else None
    env = dict(os.environ)
    env.pop("TI_ACCESS_PASSWORD", None)
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
        # 寫入秘密 + 非秘密
        _post(
            "/api/settings",
            {
                "GITHUB_TOKEN": GH,
                "ANTHROPIC_API_KEY": ANT,
                "TI_MODEL_LEAD": LEAD,
                "TI_PUBLISH_REPO": REPO,
            },
        )
        # 重新讀取
        _, raw = _get("/api/settings")
        yield raw, json.loads(raw)["fields"]
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


def test_secret_value_empty_set_true(reread):
    """驗收 #5：寫入後重讀，秘密欄位 value=='' 且 set==True。"""
    _, fields = reread
    m = _by_env(fields)
    for env in ("GITHUB_TOKEN", "ANTHROPIC_API_KEY"):
        assert m[env]["value"] == "", f"{env} 不該回顯明文"
        assert m[env]["set"] is True, f"{env} 應回報 set=True"


def test_non_secret_value_echoed(reread):
    """非秘密欄位正常回顯實際值。"""
    _, fields = reread
    m = _by_env(fields)
    assert m["TI_MODEL_LEAD"]["value"] == LEAD
    assert m["TI_PUBLISH_REPO"]["value"] == REPO


def test_no_plaintext_leak_in_response(reread):
    """防洩漏紅線：整個 GET 回應字串不含任何明文秘密值。"""
    raw, _ = reread
    assert GH not in raw, "GET 回應洩漏了 GITHUB_TOKEN 明文"
    assert ANT not in raw, "GET 回應洩漏了 ANTHROPIC_API_KEY 明文"


def test_unset_secret_still_false(reread):
    """未寫入的秘密欄位（OPENAI_API_KEY）仍 set=False、value 空。"""
    _, fields = reread
    m = _by_env(fields)
    assert m["OPENAI_API_KEY"]["value"] == ""
    assert m["OPENAI_API_KEY"]["set"] is False


def test_frontend_renders_configured_secret(reread, tmp_path):
    """前端：set=true 的秘密欄位 input.value 仍空、placeholder 切『已設定（留空＝不變更）』。"""
    node = shutil.which("node")
    if node is None:
        pytest.skip("環境無 node，略過前端渲染驗證")
    _, fields = reread
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
