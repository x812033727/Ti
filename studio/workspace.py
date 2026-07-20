"""每個 session 的沙箱工作目錄管理。專家在此目錄裡讀寫程式碼，UI 也從這裡讀取產出。"""

from __future__ import annotations

import io
import os
import shutil
import zipfile
from datetime import datetime
from pathlib import Path

from . import config

# 團隊共用知識庫檔名（跨任務知識，不算交付物，不進檔案面板/打包）。
NOTES_FILE = "NOTES.md"

# 知識沉澱檔白名單：只允許寫進 docs/ 下這幾個固定檔名（交付物，進檔案面板與打包，
# 專案模式跨場次累積）。PRD.md 由澄清階段寫 workspace 根（orchestrator._write_prd）；
# 設計決策由 ADR 模組寫根目錄 DECISIONS.md＋adr.json（見 studio/adr.py）。
KNOWLEDGE_DOCS = {"RESEARCH.md"}

# 不顯示在檔案面板的雜訊（目錄）＋共用知識庫檔＋ADR 機讀索引/鎖檔
# （ADR 的人讀版 DECISIONS.md 才是交付物，不在此列）。
_IGNORE = {
    ".git",
    "__pycache__",
    ".pytest_cache",
    "node_modules",
    ".venv",
    "venv",
    NOTES_FILE,
    "adr.json",
    "adr.lock",
}


def safe_resolve(root: Path, rel: str, *, must_exist: bool = True) -> Path | None:
    """把相對路徑 rel 安全解析到 root 之內，回傳解析後的絕對 Path；逃出範圍回 None。

    這是全專案 containment 判斷的單一真實來源：
    1. fail-fast 拒絕絕對路徑與含 `..` 的輸入；
    2. `(root/rel).resolve(strict=must_exist)` 正規化並展開 symlink；
    3. `is_relative_to(root)` 確認仍落在 root 之內。

    讀取類呼叫端傳 `must_exist=True`（strict 解析，順帶擋不存在路徑與外部 symlink）。
    `must_exist=False` 給「寫新檔」場景，避免尚未存在的目標被誤擋——注意此時 resolve
    不對「不存在的尾段」展開 symlink，故「parent 為外部 symlink、往其中寫新檔」這條
    逃逸路徑無法在此被完整擋下（已知缺口，見 tests）。
    """
    try:
        p = Path(rel)
        if p.is_absolute() or ".." in p.parts:
            return None
        root = root.resolve()
        target = (root / rel).resolve(strict=must_exist)
        if not target.is_relative_to(root):
            return None
        return target
    except (FileNotFoundError, OSError, RuntimeError, ValueError):
        # RuntimeError 涵蓋 symlink loop；其餘為不存在/權限/非法路徑。
        return None


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


def count_workspaces() -> int:
    """workspaces 根目錄下的 session 目錄數（供運維可視化；不存在回 0）。"""
    root = config.WORKSPACE_ROOT
    if not root.exists():
        return 0
    return sum(1 for p in root.iterdir() if p.is_dir())


def list_files(session_id: str) -> list[str]:
    """列出 workspace 內的相對檔案路徑（排除雜訊目錄）。

    用 os.walk 於「遍歷層」剪掉 _IGNORE 目錄（不進入 node_modules/.git 等子樹），
    而非 rglob 全樹走訪後才過濾——大 workspace 的檔案面板刷新從 O(全樹) 降到
    O(有效檔)。輸出（相對路徑、排序）與舊行為完全一致。
    """
    root = workspace_path(session_id)
    if not root.exists():
        return []
    files: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _IGNORE]  # 就地剪枝：不進入雜訊子樹
        rel_dir = Path(dirpath).relative_to(root)
        for name in filenames:
            if name in _IGNORE:
                continue
            files.append(str(rel_dir / name) if rel_dir.parts else name)
    return sorted(files)


def read_file(session_id: str, rel_path: str) -> str | None:
    """安全地讀取 workspace 內某檔案內容；超出範圍或不存在則回 None。

    讀取前先檢查檔案大小：超過 config.MAX_READ_FILE_BYTES 即拒讀回提示字串（而非全量
    read_text 載入記憶體），避免超大生成檔（log／資料集）觸發 OOM。
    """
    root = workspace_path(session_id)
    target = safe_resolve(root, rel_path)
    if target is None or not target.is_file():
        return None
    try:
        size = target.stat().st_size
        if size > config.MAX_READ_FILE_BYTES:
            return f"[檔案過大：{size} bytes 超過 {config.MAX_READ_FILE_BYTES} 上限，未載入]"
        return target.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def append_note(session_id: str, note: str) -> None:
    """把一段跨任務知識追加到 workspace 內的 NOTES.md（不存在則建立）。

    沿用 workspace_path 的路徑穿越防護；空字串忽略。NOTES.md 不會進 list_files／zip。
    """
    text = note.strip()
    if not text:
        return
    root = workspace_path(session_id)
    root.mkdir(parents=True, exist_ok=True)
    safe_root = root.resolve()
    # 寫入：檔案可能尚未存在，故 must_exist=False；保留單層判斷（固定檔名理應落在 root 下）。
    target = safe_resolve(safe_root, NOTES_FILE, must_exist=False)
    if target is None or target.parent != safe_root:
        return
    with target.open("a", encoding="utf-8") as f:
        f.write(text + "\n\n")


def read_notes(session_id: str) -> str:
    """讀回 workspace 內 NOTES.md 的全部內容；不存在或超出範圍回空字串。"""
    safe_root = workspace_path(session_id).resolve()
    target = safe_resolve(safe_root, NOTES_FILE)
    # 保留 NOTES 的特例語意：只准單層（直接落在 root 之下）。
    if target is None or target.parent != safe_root or not target.is_file():
        return ""
    try:
        return target.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def append_doc(workspace_id: str, name: str, text: str) -> None:
    """把一段知識（PRD／調研結論／設計決策）追加到 workspace 的 docs/<name>。

    僅接受 KNOWLEDGE_DOCS 白名單檔名；每段加 `## <時間>` 標頭以利跨場次追溯；
    append 模式不覆寫（與使用者產品自己的 docs/ 同名檔相撞時只會追加）。
    空字串忽略；沿用 workspace_path + safe_resolve 的路徑防護。
    """
    body = (text or "").strip()
    if not body or name not in KNOWLEDGE_DOCS:
        return
    root = workspace_path(workspace_id)
    (root / "docs").mkdir(parents=True, exist_ok=True)
    target = safe_resolve(root.resolve(), f"docs/{name}", must_exist=False)
    if target is None:
        return
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    with target.open("a", encoding="utf-8") as f:
        f.write(f"## {stamp}\n\n{body}\n\n")


def _tail_at_paragraph(text: str, max_chars: int) -> str:
    """取文字尾段（最多 max_chars 字），超長時從段落邊界（空行）起切，不腰斬句子。"""
    text = text.strip()
    if len(text) <= max_chars:
        return text
    tail = text[-max_chars:]
    cut = tail.find("\n\n")
    if 0 <= cut < len(tail) - 2:
        tail = tail[cut + 2 :]
    return tail.strip()


def read_doc_tail(workspace_id: str, name: str, max_chars: int) -> str:
    """讀回 docs/<name> 的尾段（最多 max_chars 字）；不存在／超界／非白名單回空字串。"""
    if name not in KNOWLEDGE_DOCS or max_chars <= 0:
        return ""
    target = safe_resolve(workspace_path(workspace_id).resolve(), f"docs/{name}")
    if target is None or not target.is_file():
        return ""
    try:
        return _tail_at_paragraph(target.read_text(encoding="utf-8", errors="replace"), max_chars)
    except OSError:
        return ""


def read_prd_tail(workspace_id: str, max_chars: int) -> str:
    """讀回 workspace 根目錄 PRD.md（需求澄清階段沉澱）的尾段；不存在回空字串。"""
    if max_chars <= 0:
        return ""
    safe_root = workspace_path(workspace_id).resolve()
    target = safe_resolve(safe_root, "PRD.md")
    if target is None or target.parent != safe_root or not target.is_file():
        return ""
    try:
        return _tail_at_paragraph(target.read_text(encoding="utf-8", errors="replace"), max_chars)
    except OSError:
        return ""


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
    written = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel in files:
            # 與 read_file 對齊：跳過指向沙箱外的 symlink，避免外洩。
            target = safe_resolve(safe_root, rel)
            if target is None or not target.is_file():
                continue
            try:
                if target.stat().st_size > config.MAX_READ_FILE_BYTES:
                    continue
            except OSError:
                continue
            zf.write(target, arcname=rel)
            written += 1
    if written == 0:
        return None
    return buf.getvalue()
