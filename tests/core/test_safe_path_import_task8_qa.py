"""QA 獨立驗證（任務 #8）：tools._safe_path import 包裝 safe_resolve，無循環 import。

聚焦循環 import 防護：
- 依賴方向單向 tools→workspace，workspace 不 import tools（AST 靜態確認）；
- 兩種 import 順序在『乾淨子程序』皆不死鎖、可正常載入；
- _safe_path 委派 safe_resolve、行為一致（target==root 放行）。
"""

from __future__ import annotations

import ast
import subprocess
import sys

from studio import tools, workspace


def _run(code: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30)


# --- 循環 import 防護 ---


def test_workspace_does_not_import_tools():
    """靜態確認：workspace.py 不 import studio.tools（杜絕反向依賴）。"""
    tree = ast.parse(open(workspace.__file__, encoding="utf-8").read())
    mods: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mods.append(node.module or "")
        elif isinstance(node, ast.Import):
            mods += [a.name for a in node.names]
    assert not any("tools" in m for m in mods), mods


def test_import_tools_first_clean_subprocess():
    """先 import tools（會連帶 import workspace）→ 不死鎖、退出碼 0。"""
    r = _run("import studio.tools; from studio.tools import _safe_path; print('OK')")
    assert r.returncode == 0, r.stderr
    assert "OK" in r.stdout


def test_import_workspace_first_clean_subprocess():
    """先 import workspace 再 import tools → 不死鎖、退出碼 0。"""
    r = _run(
        "import studio.workspace; import studio.tools; "
        "assert studio.tools.safe_resolve is studio.workspace.safe_resolve; print('OK')"
    )
    assert r.returncode == 0, r.stderr
    assert "OK" in r.stdout


def test_import_only_tools_resolves_safe_resolve():
    """單獨 import studio.tools 時 safe_resolve 已就緒（module-level import 成功）。"""
    r = _run(
        "from studio import tools; "
        "import tempfile, pathlib; "
        "d = pathlib.Path(tempfile.mkdtemp()); "
        "assert tools._safe_path(d, '') == d.resolve(); print('OK')"
    )
    assert r.returncode == 0, r.stderr
    assert "OK" in r.stdout


# --- 委派 + 行為一致 ---


def test_safe_path_is_thin_wrapper():
    import inspect

    src = inspect.getsource(tools._safe_path)
    assert "safe_resolve" in src
    assert "not in target.parents" not in src
    assert tools.safe_resolve is workspace.safe_resolve


def test_safe_path_target_equals_root_allowed(tmp_path):
    assert tools._safe_path(tmp_path, "") == tmp_path.resolve()


def test_safe_path_external_symlink_blocked(tmp_path):
    outside = tmp_path.parent / "out.txt"
    outside.write_text("x", encoding="utf-8")
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "leak").symlink_to(outside)
    assert tools._safe_path(ws, "leak") is None


def test_safe_path_internal_symlink_allowed(tmp_path):
    (tmp_path / "real.txt").write_text("hi", encoding="utf-8")
    (tmp_path / "link.txt").symlink_to(tmp_path / "real.txt")
    assert tools._safe_path(tmp_path, "link.txt") == (tmp_path / "real.txt").resolve()
