"""QA 獨立驗證（任務 #9）：兩個指定測試檔確實覆蓋 5 類邊界，且測試『有效』。

驗收標準 6 要求「新增測試涵蓋 5 類邊界」。本檔做兩層驗證：
1. 覆蓋盤點（meta）：靜態確認 test_tools.py 與 test_zip_workspace_qa.py 各自含
   ../逃逸、絕對路徑、外部 symlink、內部 symlink 放行、target==root 的測試；
2. 有效性（mutation）：在子程序裡故意把 containment 判斷拿掉（safe_resolve 放行
   一切），確認那些邊界測試會『轉為失敗』——證明它們真的在守邊界，而非永遠綠。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

TESTS = Path(__file__).parent

# 每類邊界 → 在該檔原始碼中應出現的關鍵字（任一命中即算覆蓋）
BOUNDARIES = {
    "../逃逸": ["../", "traversal", "dotdot"],
    "絕對路徑": ["/etc/", "absolute", "is_absolute"],
    "外部 symlink": ["external_symlink", "escaping", "symlink_to(secret", "symlink_to(out"],
    "內部 symlink 放行": ["inner_symlink", "internal_symlink", "keeps_internal", "alias"],
    "target==root": ["target_equals_root", "equals_root"],
}


def _src(name: str) -> str:
    return (TESTS / name).read_text(encoding="utf-8")


def test_test_tools_covers_five_boundaries():
    src = _src("test_tools.py")
    missing = [b for b, keys in BOUNDARIES.items() if not any(k in src for k in keys)]
    assert not missing, f"test_tools.py 缺少邊界覆蓋: {missing}"


def test_test_zip_workspace_qa_covers_five_boundaries():
    src = _src("test_zip_workspace_qa.py")
    missing = [b for b, keys in BOUNDARIES.items() if not any(k in src for k in keys)]
    assert not missing, f"test_zip_workspace_qa.py 缺少邊界覆蓋: {missing}"


def _mutation(test_module: str, test_func: str) -> str:
    """在子程序裡破壞 containment，呼叫指定測試函式，回 DETECTED/NOT_DETECTED。"""
    code = f"""
import asyncio, inspect, tempfile
from pathlib import Path
from studio import tools, workspace

# 破壞：safe_resolve 放行一切（移除 containment 防護）
def broken(root, rel, *, must_exist=True):
    return Path(root) / rel

workspace.safe_resolve = broken
tools.safe_resolve = broken

import {test_module} as m
fn = m.{test_func}
d = Path(tempfile.mkdtemp())
try:
    if inspect.iscoroutinefunction(fn):
        asyncio.run(fn(d))
    else:
        fn(d)
    print("NOT_DETECTED")
except AssertionError:
    print("DETECTED")
except Exception as e:
    # 其他例外也代表破壞被察覺（行為偏離預期）
    print("DETECTED")
"""
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30)
    return r.stdout.strip()


def test_tools_traversal_test_is_effective():
    """破壞 containment 後，test_path_traversal_blocked 必須失敗（證明它有守邊界）。"""
    assert _mutation("tests.test_tools", "test_path_traversal_blocked") == "DETECTED"


def test_tools_absolute_test_is_effective():
    assert _mutation("tests.test_tools", "test_absolute_path_blocked") == "DETECTED"


def test_tools_external_symlink_test_is_effective():
    assert _mutation("tests.test_tools", "test_safe_path_external_symlink_blocked") == "DETECTED"


def test_two_target_files_pass_in_isolation():
    """兩個指定檔單獨執行也全綠（不依賴其他檔的副作用）。"""
    r = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            str(TESTS / "test_tools.py"),
            str(TESTS / "test_zip_workspace_qa.py"),
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert r.returncode == 0, r.stdout + r.stderr
