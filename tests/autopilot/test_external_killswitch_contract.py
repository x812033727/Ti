"""外置 kill switch 契約守護（docs/RESEARCH.md 提案落地）。

契約核心：「驗證器不可被受監控者自改」。autopilot 能自改 studio/ 內的程式碼與（經
UI/settings 的 .env 通道）部分設定，因此 kill switch 的三個目標常數（PAUSE_FILE／
SERVICE／HEALTH_URL）的寫入點與設定通道必須白名單化並鎖死：
  1. `config.AUTOPILOT_PAUSE_FILE` 的寫入方法呼叫點 ⊆ 已知三處（_pause／UI pause/resume）。
  2. 三常數的引用模組 ⊆ 白名單；禁止 `from .config import <常數>` 別名寫法（會讓 AST
     白名單掃不到，強制 `config.X` 慣例）。
  3. 三個 TI_AUTOPILOT_* env key 只在 config.py 各讀一次（SSOT）。
  4. settings.py 的 UI 欄位白名單（FIELDS）不含這三鍵——UI/LLM 寫 .env 的通道改不了目標。
  5. watchdog 腳本本體外置：設定走 TI_WATCHDOG_*（不經 config.py）、不依賴被監控 runtime。

驗證邊界（明講）：本檔為結構契約守門（半閉環）——systemd unit/timer 未在 CI 實跑，
真機生效（timer 觸發、pause 檔讓主迴圈停接任務）需部署環境驗證。腳本行為本身以
subprocess 實跑黑白樣本（連續失敗觸發／成功歸零）。
"""

from __future__ import annotations

import ast
import shutil
import subprocess
from pathlib import Path

import pytest
from _repo import REPO_ROOT

STUDIO = REPO_ROOT / "studio"
WATCHDOG = REPO_ROOT / "deploy" / "ti-watchdog.sh"
TIMER = REPO_ROOT / "deploy" / "ti-watchdog.timer"
SERVICE = REPO_ROOT / "deploy" / "ti-watchdog.service"

_GUARDED = ("AUTOPILOT_PAUSE_FILE", "AUTOPILOT_SERVICE", "AUTOPILOT_HEALTH_URL")
_WRITE_METHODS = {"write_text", "write_bytes", "touch", "open", "unlink", "rename", "replace"}

# PAUSE_FILE 寫入點白名單：(檔名, 函式名)。新增第四個寫入點＝有人在給 autopilot
# 開新的自我暫停/解除通道，必須人工審視是否破壞外置 kill switch 假設。
_PAUSE_WRITERS_ALLOWED = {
    ("autopilot.py", "_pause"),
    ("routes.py", "autopilot_pause"),
    ("routes.py", "autopilot_resume"),
}

# 常數引用模組白名單（config.py 為定義處天然在列）。
_REF_ALLOWED = {
    "AUTOPILOT_PAUSE_FILE": {"config.py", "autopilot.py", "routes.py"},
    "AUTOPILOT_SERVICE": {"config.py", "deploy.py"},
    "AUTOPILOT_HEALTH_URL": {"config.py", "deploy.py"},
}


def _iter_studio_sources():
    for p in sorted(STUDIO.glob("*.py")):
        yield p, ast.parse(p.read_text(encoding="utf-8"))


def _enclosing_function(tree: ast.AST, target: ast.AST) -> str:
    """回傳 target 節點所在的最內層函式名（模組層回 '<module>'）。"""
    result = "<module>"

    class _V(ast.NodeVisitor):
        def __init__(self):
            self.stack: list[str] = []

        def generic_visit(self, node):
            nonlocal result
            if node is target:
                result = self.stack[-1] if self.stack else "<module>"
            is_fn = isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            if is_fn:
                self.stack.append(node.name)
            super().generic_visit(node)
            if is_fn:
                self.stack.pop()

    _V().visit(tree)
    return result


def _find_pause_writers(tree: ast.AST) -> list[ast.AST]:
    """找 `config.AUTOPILOT_PAUSE_FILE.<寫入方法>(...)` 的呼叫節點。"""
    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if not (isinstance(f, ast.Attribute) and f.attr in _WRITE_METHODS):
            continue
        base = f.value  # 期望 config.AUTOPILOT_PAUSE_FILE
        if (
            isinstance(base, ast.Attribute)
            and base.attr == "AUTOPILOT_PAUSE_FILE"
            and isinstance(base.value, ast.Name)
            and base.value.id == "config"
        ):
            hits.append(node)
    return hits


def test_pause_file_writers_whitelisted():
    found: set[tuple[str, str]] = set()
    for path, tree in _iter_studio_sources():
        for call in _find_pause_writers(tree):
            found.add((path.name, _enclosing_function(tree, call)))
    unexpected = found - _PAUSE_WRITERS_ALLOWED
    assert not unexpected, f"發現白名單外的 PAUSE_FILE 寫入點（外置 kill switch 假設被破壞）：{unexpected}"
    assert found == _PAUSE_WRITERS_ALLOWED, f"白名單過時，請同步：實際 {found}"


def test_pause_writer_scanner_catches_violation():
    """黑樣本對照：掃描器對假想違規真的抓得到（守豁免規則真判別力）。"""
    bad = ast.parse("from . import config\nconfig.AUTOPILOT_PAUSE_FILE.write_text('x')\n")
    assert _find_pause_writers(bad), "掃描器抓不到明顯違規——白名單守護形同虛設"


def test_guarded_constants_not_aliased_by_import():
    """禁止 `from .config import AUTOPILOT_PAUSE_FILE` 等別名寫法（AST 白名單會掃不到）。"""
    for path, tree in _iter_studio_sources():
        if path.name == "config.py":
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and "config" in node.module:
                aliased = {a.name for a in node.names} & set(_GUARDED)
                assert not aliased, f"{path.name} 以 import 別名引用受守護常數 {aliased}，請改走 config.X"


def test_guarded_constants_reference_whitelist():
    for path, tree in _iter_studio_sources():
        src = ast.dump(tree)
        for const in _GUARDED:
            if f"attr='{const}'" in src or f"id='{const}'" in src:
                assert path.name in _REF_ALLOWED[const], (
                    f"{path.name} 引用了受守護常數 {const}（白名單外）——"
                    f"kill switch 目標的消費面擴大，需人工審視"
                )


def test_env_keys_read_once_in_config_only():
    keys = [f"TI_{c}" for c in _GUARDED]
    for path in sorted(STUDIO.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        for key in keys:
            n = text.count(f'"{key}"')
            if path.name == "config.py":
                assert n == 1, f"config.py 讀取 {key} 應恰一次，實際 {n}"
            else:
                assert n == 0, f"{path.name} 私讀 {key}（違反 config SSOT 且繞過 kill switch 契約）"


def test_settings_ui_channel_excludes_killswitch_keys():
    """UI 設定通道（settings.FIELDS → 寫 .env → config.reload）不得含 kill switch 目標鍵。"""
    from studio import settings

    for key in (f"TI_{c}" for c in _GUARDED):
        assert key not in settings.ALLOWED, (
            f"settings.py 白名單含 {key}——autopilot/LLM 可經 UI 通道改 kill switch 目標"
        )


# --- watchdog 腳本外置性與實跑行為 -----------------------------------------


def _script_code_lines() -> list[str]:
    """去掉註解與空行的腳本有效行（外置性斷言只看會執行的內容）。"""
    lines = []
    for raw in WATCHDOG.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            lines.append(line)
    return lines


def test_watchdog_files_exist_and_external():
    assert WATCHDOG.is_file() and SERVICE.is_file() and TIMER.is_file()
    code = "\n".join(_script_code_lines())
    assert code.startswith("set -u") or WATCHDOG.read_text(encoding="utf-8").startswith("#!/bin/bash")
    assert "curl" in code and "PAUSE_FILE" in code
    # 外置契約：有效行不得依賴被監控 runtime（python/studio），設定不得經 config.py
    assert "python" not in code.lower(), "watchdog 不得依賴被監控對象的 runtime"
    assert "studio" not in code.lower(), "watchdog 不得 import/呼叫 studio 程式碼"
    assert "TI_WATCHDOG_" in code, "watchdog 設定應走 TI_WATCHDOG_*（不經 config.py SSOT）"
    assert "OnUnitActiveSec=5min" in TIMER.read_text(encoding="utf-8")
    assert "Type=oneshot" in SERVICE.read_text(encoding="utf-8")


_HAS_TOOLS = shutil.which("bash") and shutil.which("curl")


@pytest.mark.skipif(not _HAS_TOOLS, reason="需要 bash 與 curl")
def test_watchdog_script_behavior(tmp_path):
    """實跑黑白樣本：連續失敗達門檻 → 落 PAUSE 檔；成功 → 計數歸零、不落檔。"""
    pause = tmp_path / "AUTOPILOT_PAUSED"
    state = tmp_path / "failures"
    ok_target = tmp_path / "health.txt"
    ok_target.write_text("ok", encoding="utf-8")
    env_common = {
        "TI_WATCHDOG_PAUSE_FILE": str(pause),
        "TI_WATCHDOG_STATE_FILE": str(state),
        "TI_WATCHDOG_THRESHOLD": "3",
        "PATH": "/usr/bin:/bin:/usr/local/bin",
    }

    def run(url: str) -> None:
        subprocess.run(
            ["bash", str(WATCHDOG)],
            env={**env_common, "TI_WATCHDOG_HEALTH_URL": url},
            timeout=30,
            check=False,
        )

    bad = "http://127.0.0.1:1/nope"  # 連不上的健康檢查端點
    run(bad)
    run(bad)
    assert not pause.exists() and state.read_text().strip() == "2"  # 未達門檻不觸發

    run(f"file://{ok_target}")  # 成功一次 → 歸零（curl 支援 file://）
    assert state.read_text().strip() == "" and not pause.exists()

    run(bad)
    run(bad)
    run(bad)
    assert pause.exists(), "連續 3 次失敗應落 PAUSE 檔"
    assert "health check failed 3" in pause.read_text()

    mtime = pause.stat().st_mtime_ns
    run(bad)  # 已觸發後不重寫（保留第一次觸發現場）
    assert pause.stat().st_mtime_ns == mtime
