"""任務 #1：缺 SDK（claude_agent_sdk 未安裝）時，tests/autopilot/ 整批仍可 collect。

兩道防線：

1. **靜態守門**（最便宜的攔截點）：AST 掃 studio/ 與 tests/ 每一個 .py，禁止「未經
   TYPE_CHECKING 守護的頂層 `import claude_agent_sdk` / `from claude_agent_sdk import ...`」。
   只要有人在模組頂層裸引入 SDK，缺 SDK 的環境下該檔會在 pytest collection 期就 ImportError，
   把整批測試拖成 collection error。此測試把該回歸攔在 import 之前。

2. **行為守門**：以 `sys.modules['claude_agent_sdk'] = None` 模擬「SDK 已安裝但壞掉/缺失」
   的最嚴苛情境，在乾淨子程序跑 `pytest --collect-only tests/autopilot/`，斷言退出碼 0
   （無 collection error）。比「未安裝」更嚴苛——None 會讓 `from claude_agent_sdk import X`
   拋 ImportError 而非 ModuleNotFoundError，模擬真實 CI 的封鎖手法。

判別力（排假綠）：
- `test_scanner_flags_unguarded_import` 餵一個「故意裸引入 SDK」的暫存檔，斷言 scanner 抓到——
  證明守門真會轉紅，而非永遠放行的假綠。
- `test_scanner_ignores_guarded_imports` 餵 TYPE_CHECKING 區塊／函式內／try-except 三種合法
  守護形式，斷言 scanner 不誤殺既有 lazy import。
- `test_scanner_ignores_far_typechecking_guard` 是邊界反向樣本：TYPE_CHECKING 區塊與 SDK import
  距檔頭很遠（>12 行），證明本 scanner 走 AST「模組 body 層級」語義、不靠任何「往上 N 行」
  行距啟發式，故不會因 guard 距離遠而漏攔/誤判。
"""

from __future__ import annotations

import ast
import subprocess
import sys

from _repo import REPO_ROOT

# 任務聚焦 claude_agent_sdk；helper 仍以集合表達，日後要納入別的 SDK 只需加一個元素。
_SDK_ROOTS = {"claude_agent_sdk"}


def _unguarded_sdk_imports(source: str) -> list[str]:
    """回傳 source 中「未守護的頂層 SDK import」名稱清單；空 list 代表乾淨。

    判別語義走 AST 的『模組 body 層級』，而非行距啟發式：
      - 只檢查 `tree.body` 的直接子節點。函式/方法內的 local import 天生不是 body 直接子節點，
        自動排除（SDK 該待的地方）。
      - `if TYPE_CHECKING:` 是 body 裡的 `ast.If` 節點，其 import 在 `If.body` 內、非 body 直接
        子節點，故自動排除；不論該 If 距檔頭多遠都成立。
      - `try: import ... except ImportError:` 同理（import 在 Try 內），也是合法守護、自動排除。
    只有真正裸寫在模組頂層的 SDK import 會被列為違規。
    """
    tree = ast.parse(source)
    bad: list[str] = []
    for node in tree.body:  # 僅頂層 body 直接子節點
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name.split(".")[0] in _SDK_ROOTS:
                    bad.append(a.name)
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if root in _SDK_ROOTS:
                bad.append(node.module or "")
    return bad


def _scan_tree(root) -> dict[str, list[str]]:
    """掃 root 下所有 .py，回傳 {相對路徑: [違規 import...]}，僅含有違規者。"""
    violations: dict[str, list[str]] = {}
    for path in sorted(root.rglob("*.py")):
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        try:
            bad = _unguarded_sdk_imports(source)
        except SyntaxError:
            continue  # 非本測試職責（語法錯誤由 collection 自身會抓）
        if bad:
            violations[str(path.relative_to(REPO_ROOT))] = bad
    return violations


# --- 1) 靜態守門：studio/ 與 tests/ 頂層無未守護 SDK import ---


def test_no_unguarded_sdk_import_in_studio():
    violations = _scan_tree(REPO_ROOT / "studio")
    assert not violations, f"studio/ 出現未守護的頂層 SDK import：{violations}"


def test_no_unguarded_sdk_import_in_tests():
    violations = _scan_tree(REPO_ROOT / "tests")
    assert not violations, f"tests/ 出現未守護的頂層 SDK import：{violations}"


# --- 2) 行為守門：模擬 SDK 缺失，整批 tests/autopilot/ 仍可 collect ---


def test_autopilot_batch_collects_without_sdk():
    """以 sys.modules['claude_agent_sdk']=None 模擬 SDK 缺失，子程序跑 --collect-only。

    退出碼 0 即代表整批可 collect 而無 collection error；非 0（pytest collection error 為 2）
    代表有檔在 collection 期 import 失敗。
    """
    code = (
        "import sys; sys.modules['claude_agent_sdk'] = None; "
        "sys.exit(__import__('pytest').main("
        "['--collect-only', '-q', '-p', 'no:cacheprovider', 'tests/autopilot/']))"
    )
    r = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=180,
        cwd=str(REPO_ROOT),
    )
    assert r.returncode == 0, (
        f"缺 SDK 下 tests/autopilot/ collection 失敗（rc={r.returncode}）：\n"
        f"{r.stdout[-2000:]}\n{r.stderr[-2000:]}"
    )


# --- 3) 判別力：scanner 抓得到裸引入、不誤殺合法守護 ---

_UNGUARDED_SAMPLE = (
    "from __future__ import annotations\n"
    "import os\n"
    "from claude_agent_sdk import ClaudeSDKClient\n"  # 裸引入 → 應被攔
    "x = 1\n"
)

_GUARDED_SAMPLE = (
    "from __future__ import annotations\n"
    "from typing import TYPE_CHECKING\n"
    "if TYPE_CHECKING:\n"
    "    from claude_agent_sdk import ClaudeSDKClient  # 型別用，合法\n"
    "def speak():\n"
    "    import claude_agent_sdk  # 函式內 lazy，合法\n"
    "    return claude_agent_sdk\n"
    "def build():\n"
    "    try:\n"
    "        from claude_agent_sdk import HookMatcher  # try/except 守護，合法\n"
    "    except ImportError:\n"
    "        HookMatcher = None\n"
    "    return HookMatcher\n"
)


def test_scanner_flags_unguarded_import():
    """故意裸引入 SDK 必須被攔——證明守門有判別力、會轉紅，而非假綠放行。"""
    bad = _unguarded_sdk_imports(_UNGUARDED_SAMPLE)
    assert bad == ["claude_agent_sdk"], bad


def test_scanner_ignores_guarded_imports():
    """TYPE_CHECKING／函式內／try-except 三種合法守護皆不得被誤殺。"""
    assert _unguarded_sdk_imports(_GUARDED_SAMPLE) == []


def test_scanner_ignores_far_typechecking_guard():
    """邊界反向樣本：TYPE_CHECKING guard 距檔頭很遠（>12 行）仍不誤判。

    說明：本 scanner 走 AST『模組 body 層級』語義，不靠任何「TYPE_CHECKING 往上 N 行」
    行距啟發式，故 guard 在第幾行都不影響判別——此測試把該邊界明示出來、CI 可驗。
    """
    filler = "".join(f"_pad_{i} = {i}\n" for i in range(20))  # 把 guard 推到 ~22 行外
    far_sample = (
        "from __future__ import annotations\n"
        "from typing import TYPE_CHECKING\n" + filler + "if TYPE_CHECKING:\n"
        "    from claude_agent_sdk import ClaudeSDKClient\n"
    )
    assert _unguarded_sdk_imports(far_sample) == []
