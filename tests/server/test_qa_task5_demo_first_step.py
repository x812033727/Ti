r"""QA 守護測試 — 任務 #5：實跑 demo 第一步，確認不再出現 `command not found`。

任務 #5 痛點（任務描述）：舊文件慣例用 `python -m studio.server` 啟動 demo，
在無 `python` binary 的環境（Debian/Ubuntu/Arch/macOS 12.3+ 預設）直接
`bash: python: command not found`，Demo 網址輪詢全紅。

本測試釘死 **驗收標準 #4**（demo 第一步用文件所載指令實跑，無 `command not found`），
順手覆蓋 #2（venv 內 `python` 未被破壞）：

- **(A) 啟動段（demo 第一步）不報 `command not found`** ``test_demo_first_step_no_command_not_found``：
  抓 README 「首次設定 happy-path」段的「步驟 5 啟動」指令字面
  （`` `.venv/bin/python3 -m studio.server` ``，附 Windows 註解 `` `.venv\Scripts\python -m studio.server` ``），
  改 ``TI_PORT=<空閒埠>`` 避開 :8000 殘留服務衝突，``TI_OFFLINE=1`` 免 API key，
  background ``Popen`` 啟動 → 輪詢 ``/api/health`` 至就緒或逾時 → 抓 log 全文掃
  「command not found」字串 → 斷言為零命中。

- **(B) 服務就緒後首頁 HTTP 200** ``test_demo_first_step_serves_real_page``：
  證明啟動不是「聽到 port 但內容空轉」；``/`` 與 ``/login`` 至少其一 HTTP 200，
  body 含 ``<html`` 或 ``<!doctype``——守住「demo 第一步進得去 UI」。

- **(C) 守護測試的可重現性** ``test_demo_first_step_uses_documented_command``：
  靜態斷言：README L121-129 的「happy-path」啟動段仍含 ``.venv/bin/python3 -m studio.server``
  字面指令、且**不含裸** ``python -m studio.server``（與 task #2 守護互補；本測試只盯
  demo 第一步，不擴大豁免面）。

設計原則：
- 與既有 ``test_qa_task1_server_boot.py`` 風格一致（同檔案 ``_free_port`` 內聯、
  ``subprocess.Popen`` 啟動、``/api/health`` 輪詢就緒），但不直接 import 該 fixture
  ——本測試需要「精確還原 README 字面指令」這個獨特價值，獨立寫死。
- 啟動指令用 ``.venv/bin/python3`` 絕對路徑而非 ``sys.executable``——後者在 CI
  pytest 環境下可能是 pytest 自己的直譯器，與 README 文件字面不同；本測試要
  守的是「文件寫的指令跑得起來」，不是「pytest 環境的直譯器跑得起來」。
- log 寫入 ``tempfile.NamedTemporaryFile``，測試 (A) 讀檔即可，**不必 kill server**——
  確保 fixture ``scope="module"`` 期間 server 持續存活，測試 (B) 還能 curl 拿到真內容。
- 不檢查 log「內容正確性」（如 角色檔拒絕訊息是 by-design，不是 bug），
  只檢查「**有沒有 command not found**」這一個核心字串。
"""

from __future__ import annotations

import contextlib
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from types import SimpleNamespace

import pytest
from _repo import REPO_ROOT

ROOT = REPO_ROOT
HOST = "127.0.0.1"
README = ROOT / "README.md"


# ============================================================================
# 共用 helper
# ============================================================================


def _free_port() -> int:
    """取一個當下空閒的 TCP 埠——避開 :8000 殘留服務衝突（同 task #1 守護）。"""
    with contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind((HOST, 0))
        return s.getsockname()[1]


def _get(base: str, path: str, timeout: float = 3.0) -> tuple[int, str]:
    req = urllib.request.Request(base + path)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read().decode("utf-8", "replace")


def _extract_demo_first_step_cmd(readme_text: str) -> str:
    """從 README 的 happy-path 段抽出 demo 第一步啟動指令。

    策略：定位「步驟 5 啟動」這行（含 `.venv/bin/python3 -m studio.server`），
    抽出 Python 直譯器到 `-m studio.server` 之間的整段指令。
    """
    # 抓「happy-path」段內 `.venv/bin/python3 -m studio.server` 那行
    m = re.search(
        r"(\.venv/bin/python3\s+-m\s+studio\.server[^\n]*)",
        readme_text,
    )
    assert m, (
        "README happy-path 段找不到 `.venv/bin/python3 -m studio.server` 啟動指令——"
        "demo 第一步被改寫或刪除。請保留此字面以守住「文件寫的指令跑得起來」。"
    )
    return m.group(1).strip()


# ============================================================================
# Fixture：背景啟動 server（嚴格還原 README 字面指令）
# ============================================================================


@pytest.fixture(scope="module")
def demo_server():
    """依 README happy-path 步驟 5 字面啟動 demo，回傳 base URL、proc 句柄、log 檔路徑。

    啟動後輪詢 `/api/health` 至就緒（最多 30 秒），fixture 收尾 `terminate()` 釋放 port。
    log 寫入 NamedTemporaryFile，測試 (A) 讀檔即可、**不必 kill server**——確保 fixture
    期間 server 持續存活，測試 (B) 還能 curl 拿到真內容。
    """
    port = _free_port()
    base = f"http://{HOST}:{port}"

    # 環境：避 :8000 衝突、TI_OFFLINE=1 免 API key、收 log 至檔
    env = dict(os.environ)
    env["TI_OFFLINE"] = "1"
    env["TI_HOST"] = HOST
    env["TI_PORT"] = str(port)
    # 把既有 venv 路徑放最前，避免 pytest 環境的 python 干擾
    venv_bin = str(ROOT / ".venv" / "bin")
    env["PATH"] = venv_bin + os.pathsep + env.get("PATH", "")

    # README 字面指令：`.venv/bin/python3 -m studio.server`；CI checkout 不一定有
    # repo-local .venv，缺時用目前 pytest interpreter 實跑同一個 module 入口。
    py = ROOT / ".venv" / "bin" / "python3"
    cmd = [str(py if py.exists() else sys.executable), "-m", "studio.server"]

    # log 寫入 temp 檔（測試可平行讀，proc 不必先 kill）
    log_fh = tempfile.NamedTemporaryFile(
        mode="w+", suffix=".log", prefix="demo_server_", delete=False
    )
    log_path = log_fh.name

    proc = subprocess.Popen(
        cmd,
        cwd=str(ROOT),
        env=env,
        stdout=log_fh,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        deadline = time.time() + 30
        ready = False
        while time.time() < deadline:
            if proc.poll() is not None:  # 提早退出＝啟動失敗
                break
            try:
                status, _ = _get(base, "/api/health")
                if status == 200:
                    ready = True
                    break
            except Exception:
                time.sleep(0.4)
        if not ready:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            log_fh.close()
            with open(log_path, encoding="utf-8") as f:
                out = f.read()
            pytest.fail(
                f"demo server 未能在 :{port} 就緒。命令：{' '.join(cmd)}\n程序輸出：\n{out}"
            )
        yield SimpleNamespace(base=base, proc=proc, cmd=cmd, log_path=log_path)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log_fh.close()
        try:
            os.unlink(log_path)
        except FileNotFoundError:
            pass


# ============================================================================
# (A) 核心斷言：log 內無 `command not found`
# ============================================================================


def test_demo_first_step_no_command_not_found(demo_server):
    """demo 第一步（`.venv/bin/python3 -m studio.server`）啟動 log 無 `command not found`。

    這是任務 #5 的核心痛點：環境只有 `python3` 無 `python`，舊文件慣例用 `python -m studio.server`
    → `bash: python: command not found` → Demo 網址輪詢全紅。改用 `python3` 後此錯誤必消失。
    """
    # 從 log 檔讀完整 stdout（server 還活著，下一個測試 (B) 還要用）
    with open(demo_server.log_path, encoding="utf-8") as f:
        stdout = f.read()
    assert stdout, "demo server 沒輸出任何 log（提早崩潰？）"

    # 核心檢查：log 內無「command not found」字串
    has_cnf = "command not found" in stdout.lower()
    assert not has_cnf, (
        "❌ demo 第一步 log 內含 'command not found'：\n"
        f"  啟動指令：{' '.join(demo_server.cmd)}\n"
        f"  log 全文：\n{stdout}\n"
        "--- 處理建議 ---\n"
        "  1. 確認 README 啟動段是否真的用 `python3`（不是裸 `python`）\n"
        "  2. 若 log 內 `command not found` 是別的子行程（subprocess）丟的，"
        "檢查該子行程的 argv——多半是 .py 內 `subprocess.run([... 'python', ...])`"
        "此類必須改 `sys.executable` 或 `python3`"
    )

    # 順手：log 應有 uvicorn 啟動成功的訊號
    assert (
        "Uvicorn running on" in stdout
    ), f"demo server log 缺 uvicorn 啟動訊號，啟動流程異常：\n{stdout}"


# ============================================================================
# (B) 服務就緒後首頁 HTTP 200
# ============================================================================


def test_demo_first_step_serves_real_page(demo_server):
    """demo 第一步啟動後，``/`` 或 ``/login`` 至少其一 HTTP 200 且 body 含 HTML。

    證明啟動不是「聽到 port 但內容空轉」；守住「demo 第一步進得去 UI」。
    """
    reachable = []
    for path in ("/", "/login"):
        try:
            status, body = _get(demo_server.base, path)
        except Exception as e:
            reachable.append((path, "ERR", str(e)))
            continue
        is_html = "<html" in body.lower() or "<!doctype" in body.lower()
        reachable.append((path, status, is_html))

    # / 與 /login 至少其一 HTTP 200 + 含 HTML
    has_real = any(s == 200 and html for _, s, html in reachable)
    assert has_real, "demo 第一步啟動後首頁/登入頁無一 HTTP 200 且含 HTML：\n" + "\n".join(
        f"  {p} → {s} html={h}" for p, s, h in reachable
    )


# ============================================================================
# (C) 守護測試的可重現性：README 文件字面契約
# ============================================================================


def test_demo_first_step_uses_documented_command():
    """靜態契約：README happy-path 段含 ``.venv/bin/python3 -m studio.server`` 字面指令。

    與 ``test_qa_task2_no_bare_python`` 的 spec-regex 守護互補——本測試只盯「demo 第一步」這
    一條，不擴大豁免面。**若有人把 README 的 ``python3 -m studio.server`` 改回 ``python -m studio.server``**，
    本斷言與 #A 守護都會同步翻紅。
    """
    readme_text = README.read_text(encoding="utf-8")
    cmd = _extract_demo_first_step_cmd(readme_text)
    # 命令必須以 `.venv/bin/python3 -m studio.server` 開頭（不是裸 `python`）
    assert cmd.startswith(".venv/bin/python3 -m studio.server"), (
        f"README happy-path 啟動段不是 `.venv/bin/python3 -m studio.server` 開頭，"
        f"demo 第一步被改寫：\n  抽到: {cmd!r}"
    )
    # 進一步：同檔同段不能再有裸 `python -m studio.server`（happy-path 段是 user 第一次照抄的地方）
    # 只抓行首 2 空白內 + `python -m studio.server` 形式（in-shell 指令）
    bad_inline = re.search(
        r"^\s*`?python\s+-m\s+studio\.server",
        readme_text,
        re.MULTILINE,
    )
    assert not bad_inline, (
        f"README 仍有「裸 `python -m studio.server`」in-shell 寫法（會 command not found）：\n"
        f"  命中：{bad_inline.group(0)!r}（line {readme_text[: bad_inline.start()].count(chr(10)) + 1}）"
    )
