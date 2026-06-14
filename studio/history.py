"""Session 歷史存檔 —— 把每次工作室執行的事件落地，供日後列表與重播。

每個 session 存成兩個檔：
  history/<id>.jsonl       逐行 JSON 的事件串流（依發生順序）
  history/<id>.meta.json   摘要（需求、時間、狀態、事件數）

純檔案 IO、與 LLM 解耦，方便單元測試。
"""

from __future__ import annotations

import json
import logging
import shutil
import time
from pathlib import Path

from . import config, memory, workspace
from .secure_write import secure_write_root

log = logging.getLogger("ti.history")


def _safe_id(session_id: str) -> str:
    safe = "".join(c for c in session_id if c.isalnum() or c in "-_")
    return safe or "default"


def _events_path(session_id: str) -> Path:
    return config.HISTORY_ROOT / f"{_safe_id(session_id)}.jsonl"


def _meta_path(session_id: str) -> Path:
    return config.HISTORY_ROOT / f"{_safe_id(session_id)}.meta.json"


def start_session(session_id: str, requirement: str) -> dict:
    """建立 session 的歷史檔與初始 meta（狀態 running）。"""
    config.HISTORY_ROOT.mkdir(parents=True, exist_ok=True)
    # events.jsonl 以 secure_write_root 建立空檔（root owner），確保後續 append 接續在
    # root-owned 檔案上、不破壞 strict 不變量。
    secure_write_root(_events_path(session_id), b"")
    meta = {
        "session_id": _safe_id(session_id),
        "requirement": requirement,
        "started_at": time.time(),
        "status": "running",  # running / completed / incomplete / stopped / error
        "n_events": 0,
    }
    _write_meta(session_id, meta)
    return meta


def record_event(session_id: str, event: dict) -> None:
    """附加一則事件到 jsonl（若 session 未建立則略過）。"""
    path = _events_path(session_id)
    if not path.parent.exists():
        return
    # 守門：events 檔須由 start_session 以 secure_write_root 先建立（root-owned）。若尚未
    # 初始化就 append，open("a") 會靜默建出非 root-owned 檔，破壞 strict 不變量——讓問題早死。
    if not path.exists():
        raise RuntimeError("events.jsonl 尚未初始化，請先呼叫 start_session")
    # append 不改 owner：對既有 root-owned 檔追加仍維持 root owner，不走 secure_write_root
    # （後者為覆寫語意，用於 append 會清空整個 jsonl）。
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


def finish_session(session_id: str) -> dict | None:
    """讀回事件、推導最終狀態並更新 meta。"""
    meta = get_meta(session_id)
    if meta is None:
        return None
    events = load_events(session_id)
    meta["n_events"] = len(events)
    meta["finished_at"] = time.time()
    meta["status"] = _derive_status(events)
    parallel = _derive_parallel(events)
    if parallel:
        meta["parallel"] = parallel  # 供 /api/metrics 聚合並行可觀測性
    meta["scorecard"] = _derive_scorecard(events, meta)  # 供 /api/metrics 聚合成果記分卡
    _write_meta(session_id, meta)
    # 收尾時順手回收超量/過舊的舊 session（本場剛寫完 meta、已非 running 且為最新，不會被
    # 自己回收掉）；回收失敗絕不影響本次收尾。
    try:
        enforce_retention()
    except Exception:  # noqa: BLE001 — 回收失敗不可影響本次收尾
        pass
    return meta


def _derive_status(events: list[dict]) -> str:
    for ev in reversed(events):
        if ev.get("type") == "error":
            return "error"
    for ev in reversed(events):
        if ev.get("type") == "done":
            p = ev.get("payload", {})
            if p.get("stopped"):
                return "stopped"
            return "completed" if p.get("completed") else "incomplete"
    return "incomplete"


def _derive_scorecard(events: list[dict], meta: dict) -> dict:
    """從事件流推導本場「成果記分卡」：任務完成數、每任務輪數、退回原因分類、Demo 結果。

    這是「工作室有沒有越做越進步」的量測基礎——/api/metrics 跨場聚合成功率、平均輪數
    與近期趨勢。輪數以 task_status 的 review 次數計（每輪驗證前必進一次 review）；
    退回原因取自既有結構化事件，不解析自然語言：
      qa_fail＝run_result 失敗且非自測、smoke_fail＝自測失敗、gate_veto＝客觀閘門退回、
      critic＝異議檢查退回、stall＝停滯收斂提早結束。
    """
    tasks: dict[int, dict] = {}  # id -> {"reviews": n, "done": bool}
    rejects = {"qa_fail": 0, "smoke_fail": 0, "gate_veto": 0, "critic": 0, "stall": 0}
    huddles = huddle_limits = 0
    demo_passed: bool | None = None
    completed = stopped = False
    for ev in events:
        t = ev.get("type")
        p = ev.get("payload") or {}
        if t == "task_status":
            tid = p.get("id")
            if tid is None:
                continue
            rec = tasks.setdefault(tid, {"reviews": 0, "done": False})
            if p.get("status") == "review":
                rec["reviews"] += 1
            elif p.get("status") == "done":
                rec["done"] = True
        elif t == "run_result" and not p.get("passed"):
            # detail 以「自測」開頭＝交付前 smoke-run；其餘為 QA 驗證裁決。
            key = "smoke_fail" if str(p.get("detail", "")).startswith("自測") else "qa_fail"
            rejects[key] += 1
        elif t == "critic_review" and not p.get("passed"):
            rejects["critic"] += 1
        elif t == "huddle":
            if p.get("limitation"):
                huddle_limits += 1
            else:
                huddles += 1
        elif t == "phase_change":
            phase = p.get("phase", "")
            if phase == "客觀閘門":
                rejects["gate_veto"] += 1
            elif phase == "停滯收斂":
                rejects["stall"] += 1
        elif t == "demo_result":
            demo_passed = bool(p.get("passed"))
        elif t == "done":
            completed = bool(p.get("completed"))
            stopped = bool(p.get("stopped"))
    reviewed = [r for r in tasks.values() if r["reviews"] > 0]
    rounds_total = sum(r["reviews"] for r in reviewed)
    sc: dict = {
        "tasks_total": len(tasks),
        "tasks_done": sum(1 for r in tasks.values() if r["done"]),
        "rounds_total": rounds_total,
        "avg_rounds": round(rounds_total / len(reviewed), 2) if reviewed else 0.0,
        "first_try_done": sum(1 for r in tasks.values() if r["done"] and r["reviews"] == 1),
        "rejects": rejects,
        "huddles": huddles,
        "huddle_limits": huddle_limits,
        "demo_passed": demo_passed,
        "completed": completed,
        "stopped": stopped,
    }
    if meta.get("finished_at") and meta.get("started_at"):
        sc["duration_s"] = round(meta["finished_at"] - meta["started_at"], 1)
    return sc


def _derive_parallel(events: list[dict]) -> dict:
    """從 done 事件取出並行可觀測性摘要（無則回空 dict）。"""
    for ev in reversed(events):
        if ev.get("type") == "done":
            p = ev.get("payload", {}).get("parallel")
            return p if isinstance(p, dict) else {}
    return {}


def list_sessions() -> list[dict]:
    """回傳所有 session 的 meta，依開始時間新到舊。"""
    root = config.HISTORY_ROOT
    if not root.exists():
        return []
    metas: list[dict] = []
    for p in root.glob("*.meta.json"):
        try:
            metas.append(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    metas.sort(key=lambda m: m.get("started_at", 0), reverse=True)
    return metas


def _last_activity_ts(meta: dict) -> float:
    """session 最後活動時間：以 events 檔 mtime 為準（每則事件 append 都會更新它，O(1) 免解析）；
    取不到則退回 meta 的 finished_at / started_at。"""
    p = _events_path(meta.get("session_id", ""))
    try:
        return p.stat().st_mtime
    except OSError:
        return meta.get("finished_at") or meta.get("started_at") or 0.0


def busy_sessions(stale_after_s: float) -> list[dict]:
    """回傳『真正進行中』的 session：status==running 且最後活動在 stale_after_s 秒內。

    超過 stale_after_s 仍無動靜者視為 stale（討論崩潰／沒收尾、meta 卡在 running），
    不算 busy——否則一場死掉的討論會讓 idle 守衛永久延後部署（autodeploy timer /
    autopilot `_wait_until_idle` 共用此判定）。
    """
    now = time.time()
    return [
        m
        for m in list_sessions()
        if m.get("status") == "running" and now - _last_activity_ts(m) <= stale_after_s
    ]


def get_meta(session_id: str) -> dict | None:
    path = _meta_path(session_id)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def mark_interrupted(session_id: str, note: str = "") -> bool:
    """把卡在 running 的幽靈 meta 標為 error（服務重啟／行程被殺，finish_session 沒跑到）。

    只在 meta 存在且 status==running 時動作（冪等，不覆寫已正常收尾的場次）；
    順手補正 n_events——start_session 時寫 0，中斷後不會再有人更新，歷史列表會誤顯。
    回傳是否有改動。
    """
    meta = get_meta(session_id)
    if meta is None or meta.get("status") != "running":
        return False
    meta["status"] = "error"
    meta["finished_at"] = time.time()
    if note:
        meta["note"] = note
    meta["n_events"] = len(load_events(session_id))
    _write_meta(session_id, meta)
    return True


def load_events(session_id: str) -> list[dict]:
    path = _events_path(session_id)
    if not path.is_file():
        return []
    events: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def delete_session(session_id: str) -> bool:
    """刪除單一 session 的 meta、events 與 workspace 產出。

    拒刪 running 中的 session（避免刪掉正在跑的)；找不到 meta 回 False。
    """
    meta = get_meta(session_id)
    if meta is None:
        return False
    if meta.get("status") == "running":
        return False
    for p in (_meta_path(session_id), _events_path(session_id)):
        if p.exists():
            p.unlink()
    memory.delete(session_id)  # per-session 反思記憶（與 events/meta 同生命週期）
    ws = workspace.workspace_path(session_id)
    if ws.exists():
        shutil.rmtree(ws, ignore_errors=True)
    # 並行支線的 worktree 是 workspace 的兄弟目錄（<id>.lanes）。正常路徑於 session 收尾即清掉，
    # 此處兜底「程序中途崩潰未收尾」殘留的 .lanes，避免回收後仍永久占用磁碟。
    lanes = ws.parent / f"{ws.name}.lanes"
    if lanes.exists():
        shutil.rmtree(lanes, ignore_errors=True)
    return True


def delete_completed_sessions() -> int:
    """刪除所有 status == 'completed' 的 session,回傳刪除筆數。"""
    deleted = 0
    for meta in list_sessions():
        if meta.get("status") == "completed" and delete_session(meta["session_id"]):
            deleted += 1
    return deleted


def enforce_retention(max_count: int | None = None, max_age_s: float | None = None) -> int:
    """依保留策略回收「非 running」且超量/過舊的 session（含 meta、events 與 workspace），
    回傳實際刪除筆數。

    - 數量上限 max_count：保留最新 max_count 個非 running session，其餘較舊者刪除。
    - 年齡上限 max_age_s：最後活動（_last_activity_ts）超過 max_age_s 秒的非 running 刪除。
    兩規則取聯集（任一超標即回收）；對應上限 <=0 視為停用。running 中的 session 由
    delete_session 守門，永不刪。max_count / max_age_s 省略時讀 config 的對應預設。
    """
    max_count = config.HISTORY_MAX_COUNT if max_count is None else max_count
    max_age_s = config.HISTORY_MAX_AGE if max_age_s is None else max_age_s
    keepable = [m for m in list_sessions() if m.get("status") != "running"]  # 已新→舊
    now = time.time()
    victims: dict[str, dict] = {}  # 以 session_id 去重（兩規則可能同時命中）
    if max_count and max_count > 0:
        for m in keepable[int(max_count) :]:
            victims[m["session_id"]] = m
    if max_age_s and max_age_s > 0:
        for m in keepable:
            if now - _last_activity_ts(m) > max_age_s:
                victims[m["session_id"]] = m
    deleted = 0
    for sid in victims:
        if delete_session(sid):
            deleted += 1
    if deleted:
        log.info(
            "history 保留策略回收 %d 個 session（max_count=%s, max_age_s=%s）",
            deleted,
            max_count,
            max_age_s,
        )
    return deleted


def _write_meta(session_id: str, meta: dict) -> None:
    # 走 secure_write_root：原子 + symlink 防護 + 依 TI_REQUIRE_CHOWN 驗證 root owner。
    secure_write_root(
        _meta_path(session_id),
        json.dumps(meta, ensure_ascii=False, indent=2).encode("utf-8"),
    )
