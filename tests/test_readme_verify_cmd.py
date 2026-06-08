"""任務 #2 驗收（驗收標準 3）：README 有一行可複製的環境驗證指令，
新人照做能看到預期輸出 `ok`。除文字結構檢查外，實際以子行程跑該指令確認可執行。
"""

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
README = (ROOT / "README.md").read_text(encoding="utf-8")

VERIFY_LINUX = ".venv/bin/python3 -c \"import studio; print('ok')\""
VERIFY_WIN = ".venv\\Scripts\\python -c \"import studio; print('ok')\""


def test_verify_command_documented():
    """README 含可複製的 mac/Linux 驗證指令。"""
    assert VERIFY_LINUX in README, "缺 mac/Linux 驗證指令"


def test_verify_command_windows_parallel():
    """比照設計決策並列 Windows 路徑。"""
    assert VERIFY_WIN in README, "缺 Windows 驗證指令並列"


def test_expected_output_noted():
    """驗證指令附明確預期輸出 `ok`。"""
    # 驗證指令所在 code block 後須有「預期結果」與 ok 字樣
    idx = README.find(VERIFY_LINUX)
    tail = README[idx : idx + 400]
    assert "預期結果" in tail, "驗證指令附近缺『預期結果』"
    assert "ok" in tail


def test_verify_command_actually_runs():
    """實際執行 README 所寫的驗證邏輯，須輸出 ok、return code 0。"""
    proc = subprocess.run(
        [sys.executable, "-c", "import studio; print('ok')"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, f"驗證指令執行失敗: {proc.stderr}"
    assert proc.stdout.strip() == "ok", f"預期輸出 ok，實得: {proc.stdout!r}"


def test_uses_full_venv_path_not_bare_python():
    """驗證指令採 .venv 完整路徑（免 activate），不是裸 python。"""
    # 不可出現 `python -c "import studio` 這種裸寫法當驗證指令
    bare = re.findall(r'(?<![/\\])\bpython3? -c "import studio', README)
    assert not bare, f"驗證指令出現裸 python 寫法: {bare}"
