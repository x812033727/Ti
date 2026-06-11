"""專案（長期產品）—— 跨 session 的一級實體。

session 是一次性的討論；專案則是「同一個產品做下去」的容器：
  projects/<pid>/meta.json     專案摘要（名稱、願景、session 紀錄）
  projects/<pid>/backlog.json  專屬改良任務佇列（經 backlog.state_dir 操作）
  workspaces/project-<pid>/    固定 workspace（程式碼與 git 歷史跨場次累積）

workspace 刻意放在 WORKSPACE_ROOT 下（id 為 `project-<pid>`），讓既有的
/api/workspace/{id}/files、/file、/download 與前端檔案面板零改動直接可用；
history 保留策略只回收「有對應 session meta」的 workspace，碰不到專案目錄。

純檔案 IO、與 LLM 解耦，方便單元測試（測試時用 TI_PROJECTS_ROOT 指向 tmp）。
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

from . import config, workspace


def _safe_id(project_id: str) -> str:
    safe = "".join(c for c in project_id if c.isalnum() or c in "-_")
    return safe or "default"


def _dir(project_id: str) -> Path:
    return config.PROJECTS_ROOT / _safe_id(project_id)


def _meta_path(project_id: str) -> Path:
    return _dir(project_id) / "meta.json"


def state_dir(project_id: str) -> Path:
    """該專案 backlog 的 state 目錄（傳給 backlog.* 的 state_dir）。"""
    return _dir(project_id)


def workspace_id(project_id: str) -> str:
    """專案固定 workspace 在 WORKSPACE_ROOT 下的 id（給檔案面板/下載 API 用）。"""
    return f"project-{_safe_id(project_id)}"


def workspace_dir(project_id: str) -> Path:
    """專案固定 workspace 路徑（不存在則建立；絕不清空既有內容）。"""
    path = workspace.workspace_path(workspace_id(project_id))
    path.mkdir(parents=True, exist_ok=True)
    return path


def create(name: str, vision: str = "") -> dict | None:
    """建立新專案（名稱必填），回傳 meta；名稱為空回 None。"""
    name = (name or "").strip()
    if not name:
        return None
    pid = uuid.uuid4().hex[:12]
    meta = {
        "id": pid,
        "name": name,
        "vision": (vision or "").strip(),
        "created_at": time.time(),
        "updated_at": time.time(),
        "sessions": [],  # [{session_id, task, completed, at}]
    }
    _dir(pid).mkdir(parents=True, exist_ok=True)
    _write_meta(pid, meta)
    workspace_dir(pid)  # 一併備妥固定 workspace
    return meta


def get(project_id: str) -> dict | None:
    p = _meta_path(project_id)
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def list_projects() -> list[dict]:
    """所有專案的 meta，依建立時間新到舊。"""
    root = config.PROJECTS_ROOT
    if not root.exists():
        return []
    metas: list[dict] = []
    for p in root.glob("*/meta.json"):
        try:
            metas.append(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    metas.sort(key=lambda m: m.get("created_at", 0), reverse=True)
    return metas


def record_session(project_id: str, session_id: str, task: str, completed: bool) -> dict | None:
    """把一場討論的結果記到專案 meta（持續改良的足跡），回傳更新後 meta。"""
    meta = get(project_id)
    if meta is None:
        return None
    meta.setdefault("sessions", []).append(
        {"session_id": session_id, "task": task, "completed": completed, "at": time.time()}
    )
    meta["updated_at"] = time.time()
    _write_meta(project_id, meta)
    return meta


def _write_meta(project_id: str, meta: dict) -> None:
    _meta_path(project_id).write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
