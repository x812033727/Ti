"""任務級反思記憶 —— 補主迴圈「只帶上一輪原文回饋」的缺口。

每輪失敗時，把 QA／高級工程師的意見蒸餾成一段精簡反思（見 reflexion.py），存進
per-session 的 JSONL；後續輪次／huddle 重試前，依任務撈回先前輪次的反思 prepend 進工程師
context，讓同一任務跨輪累積經驗、不重蹈覆轍。

職責邊界：本模組只管「同一 session 內、同一任務跨輪」的短期記憶；跨場次的長期教訓仍歸
lessons.py（兩者不重疊）。存法沿用 lessons.py：單檔 + fcntl 檔案鎖序列化 append，跨程序安全
（多 session／多 uvicorn worker／並行 lane 皆可安全增寫）。純檔案 IO、與 LLM 解耦，易測。

移植自 ti-studio 自我進步交付的 memory.py（去重保最新 + recent_n + token 預算 + 預算為 0
回空守衛 + 0600 權限），把 threading.Lock 換成跨程序的 fcntl 檔案鎖。
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import time
from pathlib import Path

from . import config

# 約略 token 估算：中文偏密，保守用 2.5 char/token（注入端只需「夠用的上界」防爆預算，非精準）。
_CHARS_PER_TOKEN = 2.5

DEFAULT_HEADER = "【過往反思（本任務先前輪次蒸餾，請避免重蹈覆轍）】"

_TEMPLATE_MAX_STORE = 50
_TEMPLATE_CONTEXT_MAX = 3
_TEMPLATE_FEATURE_MAX = 6
_TEMPLATE_DECISION_MAX = 5
_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*|[\u4e00-\u9fff]{2,}")
_STOP_TOKENS = {
    "產品",
    "專案",
    "功能",
    "核心",
    "使用",
    "使用者",
    "工具",
    "系統",
    "平台",
    "管理",
    "改良",
    "體驗",
    "支援",
}


def _safe_id(session_id: str) -> str:
    safe = "".join(c for c in session_id if c.isalnum() or c in "-_")
    return safe or "default"


def memory_path(session_id: str) -> Path:
    return config.HISTORY_ROOT / f"{_safe_id(session_id)}.memory.jsonl"


def _lock_path(session_id: str) -> Path:
    return config.HISTORY_ROOT / f"{_safe_id(session_id)}.memory.lock"


def template_path() -> Path:
    return config.PROJECTS_ROOT / "project_templates.json"


def _template_lock_path() -> Path:
    return config.PROJECTS_ROOT / "project_templates.lock"


@contextlib.contextmanager
def _locked(session_id: str):
    """以獨立 lock 檔序列化 append，跨程序／並行 lane 安全。"""
    config.HISTORY_ROOT.mkdir(parents=True, exist_ok=True)
    lock = _lock_path(session_id).open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


def write(
    session_id: str,
    task_id,
    content: str,
    *,
    round_no: int,
    kind: str = "reflection",
    meta: dict | None = None,
) -> bool:
    """附加一筆反思（空白內容略過）。回傳是否實際寫入。"""
    content = (content or "").strip()
    if not content:
        return False
    record = {
        "task_id": task_id,
        "round": round_no,
        "content": content,
        "kind": kind,
        "ts": time.time(),
        "meta": meta,
    }
    line = json.dumps(record, ensure_ascii=False)
    with _locked(session_id):
        path = memory_path(session_id)
        new_file = not path.exists()
        with path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        if new_file:
            # 反思可能含任務敏感資訊，限制檔案權限為 0600（僅擁有者讀寫）。
            with contextlib.suppress(OSError):
                os.chmod(path, 0o600)
    return True


def _read_all(session_id: str) -> list[dict]:
    path = memory_path(session_id)
    if not path.exists():
        return []
    records: list[dict] = []
    # 讀不加鎖（與 append 並發最多讀到尚未寫完的壞行，逐行 try 跳過即可）。
    for raw in path.read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            d = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(d, dict):
            records.append(d)
    return records


def retrieve(
    session_id: str,
    task_id=None,
    *,
    kind: str | None = "reflection",
    before_round: int | None = None,
) -> list[dict]:
    """撈回反思（依寫入序，舊→新）。

    - task_id=None：不依任務過濾（取全部）。
    - before_round：只取 round 小於此值的記錄（供 huddle 重試編號避碰）。
    """
    records = _read_all(session_id)
    if task_id is not None:
        records = [r for r in records if r.get("task_id") == task_id]
    if kind is not None:
        records = [r for r in records if r.get("kind") == kind]
    if before_round is not None:
        records = [r for r in records if (r.get("round") or 0) < before_round]
    return records


def build_context(
    session_id: str,
    task_id,
    *,
    exclude_latest: bool = True,
    recent_n: int | None = None,
    token_budget: int = 800,
    header: str = DEFAULT_HEADER,
) -> str:
    """組出可 prepend 進工程師 prompt 的反思區塊；停用、無記憶或預算為 0 時回 ""。

    步驟：撈回 → （可選）排除最新一筆 → 去重（保最新）→ 取最近 recent_n →
    token 預算內由新到舊納入 → 依時間序輸出，尾帶兩個換行可直接前接後文。

    exclude_latest：最新一筆＝上一輪的反思，其原文已由 _work_task 的 verbatim feedback 帶入，
    預設排除以免重複；huddle 重試（seed 為 huddle 結論、非上一輪 QA 報告）則傳 False 全帶。
    """
    if not config.REFLEXION_ENABLED:
        return ""
    records = retrieve(session_id, task_id)
    if exclude_latest and records:
        records = records[:-1]
    if not records:
        return ""
    cap = config.REFLEXION_MAX if recent_n is None else recent_n
    if cap <= 0:  # 預算為 0＝關閉注入，不可退化成全帶
        return ""

    # 去重：同 content 只留最後一次出現（最新）。
    seen: set[str] = set()
    deduped: list[dict] = []
    for r in reversed(records):
        key = (r.get("content") or "").strip()
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(r)
    deduped = deduped[:cap]  # 目前新→舊，取最近 cap 筆

    # 在 token 預算內由新到舊納入。
    char_budget = int(max(0, token_budget) * _CHARS_PER_TOKEN)
    chosen: list[dict] = []
    used = len(header) + 1
    for r in deduped:
        item_len = len(r["content"]) + 4  # "- " 前綴與換行概估
        if used + item_len > char_budget and chosen:
            break
        chosen.append(r)
        used += item_len
    if not chosen:
        return ""

    chosen.reverse()  # 依時間序（舊→新）讀起來自然
    lines = [header]
    lines.extend(f"- {r['content'].strip()}" for r in chosen)
    return "\n".join(lines) + "\n\n"


def delete(session_id: str) -> None:
    """刪除該 session 的記憶與 lock 檔（供 history GC 回收，避免洩漏）。"""
    for p in (memory_path(session_id), _lock_path(session_id)):
        with contextlib.suppress(OSError):
            if p.exists():
                p.unlink()


@contextlib.contextmanager
def _template_locked():
    """序列化全域模板記憶寫入；讀取端容錯即可，不加鎖。"""
    config.PROJECTS_ROOT.mkdir(parents=True, exist_ok=True)
    lock = _template_lock_path().open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


def _template_tokens(*parts: str) -> list[str]:
    """粗略專案類型 token：英文詞＋中文 2/3-gram，供同類型模板比對。"""
    tokens: set[str] = set()
    for raw in _TOKEN_RE.findall(" ".join(p for p in parts if p).lower()):
        if re.fullmatch(r"[\u4e00-\u9fff]{2,}", raw):
            if raw not in _STOP_TOKENS:
                tokens.add(raw)
            for n in (2, 3):
                for i in range(0, max(0, len(raw) - n + 1)):
                    gram = raw[i : i + n]
                    if gram not in _STOP_TOKENS:
                        tokens.add(gram)
        else:
            token = raw.strip("-_")
            if token and token not in _STOP_TOKENS:
                tokens.add(token)
    return sorted(tokens)


def _load_templates() -> dict:
    p = template_path()
    if not p.is_file():
        return {"version": 1, "templates": []}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("templates"), list):
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return {"version": 1, "templates": []}


def _save_templates(data: dict) -> None:
    config.PROJECTS_ROOT.mkdir(parents=True, exist_ok=True)
    tmp = template_path().with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(template_path())


def _build_project_template(project: dict) -> dict | None:
    """把單一專案既有 blueprint/ADR 摘成可注入其他專案的模板。"""
    from . import adr, blueprint, projects

    pid = str(project.get("id") or "")
    if not pid:
        return None
    data = blueprint.load(pid)
    if not data or data.get("raw") or not data.get("features"):
        return None
    features = sorted(data.get("features") or [], key=lambda f: f.get("priority", 1))[
        :_TEMPLATE_FEATURE_MAX
    ]
    decisions = [
        e.get("decision", "").strip()
        for e in adr.all_entries(projects.workspace_dir(pid))[-_TEMPLATE_DECISION_MAX:]
        if e.get("decision")
    ]
    tokens = _template_tokens(
        str(project.get("name") or ""),
        str(project.get("vision") or ""),
        str(data.get("vision") or ""),
        str(data.get("users") or ""),
        " ".join(str(f.get("title") or "") for f in features),
    )
    if not tokens:
        return None
    body = json.dumps(
        {
            "project_id": pid,
            "features": [f.get("title", "") for f in features],
            "decisions": decisions,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return {
        "id": hashlib.sha1(body.encode("utf-8")).hexdigest()[:12],
        "project_id": pid,
        "project_name": str(project.get("name") or pid),
        "tokens": tokens,
        "features": [
            {
                "title": str(f.get("title") or "").strip(),
                "priority": f.get("priority", 1),
                "detail": str(f.get("detail") or "").strip(),
            }
            for f in features
            if str(f.get("title") or "").strip()
        ],
        "decisions": decisions,
        "updated_at": time.time(),
    }


def refresh_project_template(project_id: str) -> bool:
    """把專案目前 blueprint/ADR 更新進全域模板庫；缺資料時安靜跳過。"""
    from . import projects

    project = projects.get(project_id)
    template = _build_project_template(project or {})
    if template is None:
        return False
    with _template_locked():
        data = _load_templates()
        rows = [
            r
            for r in data.get("templates", [])
            if isinstance(r, dict) and r.get("project_id") != template["project_id"]
        ]
        rows.append(template)
        rows.sort(key=lambda r: r.get("updated_at", 0))
        data = {"version": 1, "templates": rows[-_TEMPLATE_MAX_STORE:]}
        _save_templates(data)
    return True


def refresh_all_project_templates() -> int:
    """從既有專案回填模板庫；回傳成功萃取的專案數。"""
    from . import projects

    n = 0
    for project in projects.list_projects():
        if refresh_project_template(project.get("id", "")):
            n += 1
    return n


def template_context(project: dict, *, limit: int = _TEMPLATE_CONTEXT_MAX) -> str:
    """為新藍圖生成注入同類型專案模板；無相似樣本回空字串。"""
    if not config.BLUEPRINT_ENABLED or limit <= 0:
        return ""
    target = set(
        _template_tokens(
            str(project.get("name") or ""),
            str(project.get("vision") or ""),
            str(project.get("intent") or ""),
        )
    )
    if not target:
        return ""
    current_id = str(project.get("id") or "")
    data = _load_templates()
    if not data.get("templates"):
        with contextlib.suppress(Exception):
            refresh_all_project_templates()
        data = _load_templates()
    ranked: list[tuple[int, float, dict]] = []
    for row in data.get("templates", []):
        if not isinstance(row, dict) or row.get("project_id") == current_id:
            continue
        overlap = len(target & set(row.get("tokens") or []))
        if overlap <= 0:
            continue
        ranked.append((overlap, float(row.get("updated_at") or 0), row))
    if not ranked:
        return ""
    ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
    lines = ["【跨專案模板記憶（同類型產品既有藍圖/ADR，請復用可行模式，不要硬套）】"]
    for _, _, row in ranked[:limit]:
        lines.append(f"- 參考專案：{row.get('project_name') or row.get('project_id')}")
        features = [
            str(f.get("title") or "").strip()
            for f in row.get("features") or []
            if isinstance(f, dict) and str(f.get("title") or "").strip()
        ][:3]
        if features:
            lines.append(f"  藍圖模式：{'；'.join(features)}")
        decisions = [str(d).strip() for d in row.get("decisions") or [] if str(d).strip()][:3]
        if decisions:
            lines.append(f"  既有決策：{'；'.join(decisions)}")
    text = "\n".join(lines)
    if "任務:" in text or "核心改動:" in text:
        text = text.replace("任務:", "工作項目:").replace("核心改動:", "框架改動:")
    return text[:1200] + "\n\n"
