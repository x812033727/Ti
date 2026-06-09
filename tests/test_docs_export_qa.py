"""QA：驗證 README / ARCHITECTURE 的匯出說明存在且與實際 API 一致（任務 #4）。"""

from __future__ import annotations

import pytest
from _repo import REPO_ROOT

ROOT = REPO_ROOT
README = (ROOT / "README.md").read_text(encoding="utf-8")
ARCH = (ROOT / "ARCHITECTURE.md").read_text(encoding="utf-8")


@pytest.mark.parametrize("kw", ["下載成果", "zip", ".git"])
def test_readme_mentions_export(kw):
    assert kw in README, f"README 缺少匯出說明關鍵字: {kw}"


@pytest.mark.parametrize(
    "kw",
    ["/api/workspace/{session_id}/download", "application/zip", "require_auth", "zip_workspace"],
)
def test_architecture_describes_route(kw):
    assert kw in ARCH, f"ARCHITECTURE 缺少: {kw}"


def test_docs_route_matches_real_routes():
    """文件描述的路由與 media_type 必須與 routes.py 實際實作一致。"""
    routes_src = (ROOT / "studio" / "routes.py").read_text(encoding="utf-8")
    assert "/api/workspace/{session_id}/download" in routes_src
    assert "application/zip" in routes_src
    # 文件宣稱掛 require_auth，程式碼也必須真的掛上
    assert "require_auth" in routes_src


def test_docs_filename_pattern_matches_impl():
    """文件描述檔名 workspace-<session_id>.zip，需與路由實際組出的檔名一致。"""
    routes_src = (ROOT / "studio" / "routes.py").read_text(encoding="utf-8")
    assert 'filename="workspace-' in routes_src
    assert "workspace-<session_id>.zip" in README or "workspace-" in README
