"""每個 session 的沙箱工作目錄管理。專家在此目錄裡讀寫程式碼，UI 也從這裡讀取產出。"""

from __future__ import annotations

import io
import shutil
import zipfile
from pathlib import Path

from . import config

# 不顯示在檔案面板的雜訊
_IGNORE = {".git", "__pycache__", ".pytest_cache", "node_modules", ".venv", "venv"}


def create_workspace(session_id: str) -> Path:
    """建立（或清空重建）一個乾淨的 session 工作目錄，回傳其路徑。"""
    path = workspace_path(session_id)
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)
    return path


def workspace_path(session_id: str) -> Path:
    # 防止路徑穿越：只取最後一段、過濾危險字元。
    safe = "".join(c for c in session_id if c.isalnum() or c in "-_")
    return config.WORKSPACE_ROOT / (safe or "default")


def list_files(session_id: str) -> list[str]:
    """列出 workspace 內的相對檔案路徑（排除雜訊目錄）。"""
    root = workspace_path(session_id)
    if not root.exists():
        return []
    files: list[str] = []
    for p in sorted(root.rglob("*")):
        if p.is_dir():
            continue
        rel_parts = p.relative_to(root).parts
        if any(part in _IGNORE for part in rel_parts):
            continue
        files.append(str(p.relative_to(root)))
    return files


def read_file(session_id: str, rel_path: str) -> str | None:
    """安全地讀取 workspace 內某檔案內容；超出範圍或不存在則回 None。"""
    root = workspace_path(session_id).resolve()
    target = (root / rel_path).resolve()
    if root not in target.parents and target != root:
        return None  # 路徑穿越
    if not target.is_file():
        return None
    try:
        return target.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def zip_workspace(session_id: str) -> bytes | None:
    """把該 session 的 workspace 打包成 zip（bytes）。

    內容沿用 list_files()，因此自動排除 .git / __pycache__ 等雜訊目錄；
    workspace 不存在或無任何產出檔案時回 None。所有寫入路徑都在
    workspace_path() 之內，不會外洩沙箱以外檔案。
    """
    root = workspace_path(session_id)
    if not root.exists():
        return None
    files = list_files(session_id)
    if not files:
        return None
    safe_root = root.resolve()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel in files:
            target = (root / rel).resolve()
            # 與 read_file 對齊：跳過指向沙箱外的 symlink，避免外洩。
            if safe_root not in target.parents:
                continue
            zf.write(target, arcname=rel)
    return buf.getvalue()
