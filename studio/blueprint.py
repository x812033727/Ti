"""產品藍圖 —— 專案級的活規劃文件（願景 / 用戶 / 功能清單 / 里程碑）。

把「一句產品願景」展開成結構化藍圖，讓持續改良迴圈有方向感：
  projects/<pid>/blueprint.json      機讀（功能優先級餵 backlog、跨場注入 context）
  workspaces/project-<pid>/BLUEPRINT.md  人讀（進檔案面板、git 歷史與交付物）

與 workspace/PRD.md 並存不混用：PRD.md 是 session 級「需求＋澄清問答」的 append-only
日誌（orchestrator._write_prd）；藍圖是 project 級規劃，由 improver 開跑時 lazy 生成一次。

存法與 lessons/backlog 一致：JSON 檔 + 檔案鎖序列化寫入。純檔案 IO、與 LLM 解耦，
方便單元測試（測試時用 TI_PROJECTS_ROOT / TI_WORKSPACE_ROOT 指向 tmp）。
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import re
import time
from pathlib import Path

from . import backlog, config, projects

# 注入 prompt 的功能/里程碑條數上限，防藍圖過長把 requirement 前綴撐爆。
_CONTEXT_MAX_FEATURES = 10
_CONTEXT_MAX_MILESTONES = 4

_PRIORITY_LABEL = {0: "P0", 1: "P1", 2: "P2"}

# 行標記解析（沿用全 codebase「任務:/教訓:/設計決策:」的行格式慣例）。
# 功能行：`功能: [P0] <名稱> — <一句說明>`；tag 與說明皆可省略（tag 缺省 → P1）。
_RE_FEATURE = re.compile(
    r"^\s*功能\s*[:：]\s*(?:\[?(P[0-2])\]?\s*)?(.+?)(?:\s*[—–\-|]\s*(.+))?\s*$"
)
_RE_VISION = re.compile(r"^\s*願景\s*[:：]\s*(.+?)\s*$")
_RE_USERS = re.compile(r"^\s*用戶\s*[:：]\s*(.+?)\s*$")
_RE_MILESTONE = re.compile(r"^\s*里程碑\s*[:：]\s*(.+?)\s*$")


def _json_path(project_id: str) -> Path:
    return projects.state_dir(project_id) / "blueprint.json"


def _lock_path(project_id: str) -> Path:
    return projects.state_dir(project_id) / "blueprint.lock"


@contextlib.contextmanager
def _locked(project_id: str):
    """以獨立 lock 檔序列化寫入，跨程序安全（API 程序與改良迴圈可能並存）。"""
    projects.state_dir(project_id).mkdir(parents=True, exist_ok=True)
    lock = _lock_path(project_id).open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


def parse_blueprint(text: str) -> dict | None:
    """從 PM 輸出解析藍圖；連一行 `功能:` 都沒有時回 None（呼叫端走 raw fallback）。"""
    vision = users = ""
    features: list[dict] = []
    milestones: list[dict] = []
    for line in (text or "").splitlines():
        m = _RE_FEATURE.match(line)
        if m:
            tag, title, detail = m.group(1), m.group(2).strip(), (m.group(3) or "").strip()
            if title:
                priority = int(tag[1]) if tag else backlog.DEFAULT_PRIORITY
                features.append({"title": title, "priority": priority, "detail": detail})
            continue
        m = _RE_VISION.match(line)
        if m:
            vision = vision or m.group(1)
            continue
        m = _RE_USERS.match(line)
        if m:
            users = users or m.group(1)
            continue
        m = _RE_MILESTONE.match(line)
        if m:
            milestones.append({"title": m.group(1)})
    if not features:
        return None
    return {
        "version": 1,
        "vision": vision,
        "users": users,
        "features": features,
        "milestones": milestones,
    }


def save(project_id: str, data: dict, *, session_id: str = "") -> None:
    """寫 blueprint.json（補時間戳與來源 session）。"""
    data = {**data, "generated_at": time.time(), "session_id": session_id}
    with _locked(project_id):
        path = _json_path(project_id)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)


def load(project_id: str) -> dict | None:
    """讀 blueprint.json；缺檔或壞 JSON 回 None（同 projects.get 的容錯語意）。"""
    p = _json_path(project_id)
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def exists(project_id: str) -> bool:
    return _json_path(project_id).is_file()


def render_md(data: dict, *, name: str = "") -> str:
    """結構化藍圖 → BLUEPRINT.md 內容（人讀；raw fallback 時呼叫端直接寫原文）。"""
    lines = [f"# 產品藍圖{('：' + name) if name else ''}", ""]
    if data.get("vision"):
        lines += ["## 願景", "", data["vision"], ""]
    if data.get("users"):
        lines += ["## 目標用戶", "", data["users"], ""]
    feats = data.get("features") or []
    if feats:
        lines += ["## 核心功能", ""]
        for f in sorted(feats, key=lambda x: x.get("priority", backlog.DEFAULT_PRIORITY)):
            label = _PRIORITY_LABEL.get(f.get("priority", backlog.DEFAULT_PRIORITY), "P1")
            detail = f" — {f['detail']}" if f.get("detail") else ""
            lines.append(f"- **[{label}]** {f['title']}{detail}")
        lines.append("")
    miles = data.get("milestones") or []
    if miles:
        lines += ["## 里程碑", ""]
        lines += [f"- {m['title']}" for m in miles]
        lines.append("")
    return "\n".join(lines)


def write_md(project_id: str, content: str) -> None:
    """把藍圖內容寫進專案 workspace 的 BLUEPRINT.md（覆寫：藍圖是活文件、非日誌）。"""
    path = projects.workspace_dir(project_id) / "BLUEPRINT.md"
    try:
        path.write_text(content, encoding="utf-8")
    except OSError:
        pass  # 與 _write_prd 同語意：落檔失敗不擋流程


def context(project_id: str) -> str:
    """組成要注入 requirement 前綴的藍圖區塊；停用、無藍圖或 raw fallback 時回 ""。"""
    if not config.BLUEPRINT_ENABLED:
        return ""
    data = load(project_id)
    if not data or not data.get("features"):
        return ""
    lines = ["【產品藍圖（本專案的長期方向，改良任務應對齊它）】"]
    if data.get("vision"):
        lines.append(f"願景：{data['vision']}")
    if data.get("users"):
        lines.append(f"目標用戶：{data['users']}")
    feats = sorted(data["features"], key=lambda x: x.get("priority", backlog.DEFAULT_PRIORITY))[
        :_CONTEXT_MAX_FEATURES
    ]
    lines.append("核心功能（P0 必須 → P2 加分）：")
    for f in feats:
        label = _PRIORITY_LABEL.get(f.get("priority", backlog.DEFAULT_PRIORITY), "P1")
        detail = f" — {f['detail']}" if f.get("detail") else ""
        lines.append(f"- [{label}] {f['title']}{detail}")
    miles = (data.get("milestones") or [])[:_CONTEXT_MAX_MILESTONES]
    if miles:
        lines.append("里程碑：" + "；".join(m["title"] for m in miles))
    return "\n".join(lines) + "\n\n"


def seed_backlog(project_id: str, data: dict, cap: int) -> int:
    """把藍圖功能清單餵進專案 backlog（P0 先、最多 cap 筆），回實際新增數。

    走 backlog.add 既有「同標題仍 pending/in_progress 視為重複」去重；
    已餵過的功能標記 seeded，重跑不重複。
    """
    feats = sorted(
        data.get("features") or [], key=lambda x: x.get("priority", backlog.DEFAULT_PRIORITY)
    )
    sdir = projects.state_dir(project_id)
    n = 0
    for f in feats:
        if n >= cap or f.get("seeded"):
            continue
        added = backlog.add(
            f["title"],
            f.get("detail", ""),
            source="blueprint",
            state_dir=sdir,
            priority=f.get("priority", backlog.DEFAULT_PRIORITY),
            item_type="feature",
        )
        if added:
            f["seeded"] = True
            n += 1
    return n
