"""Session 歷史存檔 —— 把每次工作室執行的事件落地，供日後列表與重播。

每個 session 存成兩個檔：
  history/<id>.jsonl       逐行 JSON 的事件串流（依發生順序）
  history/<id>.meta.json   摘要（需求、時間、狀態、事件數）

純檔案 IO、與 LLM 解耦，方便單元測試。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import time
from collections.abc import Iterator
from pathlib import Path

from . import config, memory, secure_write, workspace

# 唯一 choke point：兩條後端路徑（history meta/events、backlog.json）皆經
# secure_write.secure_write_root。module-level alias 兼顧可被測試 monkeypatch。
secure_write_root = secure_write.secure_write_root

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
    meta["token_usage"] = _derive_token_usage(events)  # 供 /api/usage 聚合 provider/model 成本
    meta["latency"] = _derive_latency(events)  # 供 /api/metrics 聚合 wall-clock 時延（與 token_usage 平行）
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
    退回原因與測試／審查分子分母取自既有結構化事件，不解析自然語言：
      qa_fail＝run_result 失敗且非自測、smoke_fail＝自測失敗、gate_veto＝客觀閘門退回、
      critic＝異議檢查退回、stall＝停滯收斂提早結束。
    """
    tasks: dict[int, dict] = {}  # id -> {"reviews": n, "done": bool}
    rejects = {"qa_fail": 0, "smoke_fail": 0, "gate_veto": 0, "critic": 0, "stall": 0}
    qa_total = qa_pass = 0
    critic_total = critic_pass = 0
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
        elif t == "run_result":
            # detail 以「自測」開頭＝交付前 smoke-run；其餘為 QA 驗證裁決。
            is_smoke = str(p.get("detail", "")).startswith("自測")
            if not is_smoke:
                qa_total += 1
                if p.get("passed") is True:
                    qa_pass += 1
            if not p.get("passed"):
                rejects["smoke_fail" if is_smoke else "qa_fail"] += 1
        elif t == "critic_review":
            critic_total += 1
            if p.get("passed") is True:
                critic_pass += 1
            if not p.get("passed"):
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
        "qa_total": qa_total,
        "qa_pass": qa_pass,
        "critic_total": critic_total,
        "critic_pass": critic_pass,
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


def _blank_token_usage() -> dict:
    # cache_read／cache_write：provider（Claude Agent SDK／OpenAI）回報的 prompt-cache 命中與寫入量。
    # input_tokens（prompt）與快取 token 由 SDK 分開計列，故另立欄位、不混入 prompt，供量測快取成效。
    return {
        "prompt": 0,
        "completion": 0,
        "total": 0,
        "cost_usd": 0.0,
        "calls": 0,
        "cache_read": 0,
        "cache_write": 0,
    }


def _int_token(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _add_token_usage(
    dst: dict,
    prompt: int,
    completion: int,
    total: int,
    cost_usd,
    cache_read: int = 0,
    cache_write: int = 0,
) -> None:
    dst["prompt"] += prompt
    dst["completion"] += completion
    dst["total"] += total
    dst["cache_read"] += cache_read
    dst["cache_write"] += cache_write
    if cost_usd is not None:
        try:
            dst["cost_usd"] += float(cost_usd)
        except (TypeError, ValueError):
            pass
    dst["calls"] += 1


def _derive_token_usage(events: list[dict]) -> dict:
    """從 token_usage 事件彙總 provider/model/role 維度的用量。

    cost_usd 只有 provider SDK 回報時才加總；None 代表未知成本，不影響 token 計數。
    """
    total = _blank_token_usage()
    by_provider: dict[str, dict] = {}
    by_model: dict[str, dict] = {}
    by_role: dict[str, dict] = {}
    for ev in events:
        if ev.get("type") != "token_usage":
            continue
        p = ev.get("payload") or {}
        prompt = _int_token(p.get("prompt_tokens"))
        completion = _int_token(p.get("completion_tokens"))
        event_total = _int_token(p.get("total_tokens")) or prompt + completion
        cache_read = _int_token(p.get("cache_read"))
        cache_write = _int_token(p.get("cache_write"))
        cost_usd = p.get("cost_usd")
        provider = str(p.get("provider") or "unknown")
        model = str(p.get("model") or "unknown")
        role = str(p.get("speaker") or "unknown")
        for bucket in (
            total,
            by_provider.setdefault(provider, _blank_token_usage()),
            by_model.setdefault(model, _blank_token_usage()),
            by_role.setdefault(role, _blank_token_usage()),
        ):
            _add_token_usage(
                bucket, prompt, completion, event_total, cost_usd, cache_read, cache_write
            )
    return {
        "total": total,
        "by_provider": by_provider,
        "by_model": by_model,
        "by_role": by_role,
    }


def _blank_latency() -> dict:
    return {"count": 0, "sum_ms": 0, "max_ms": 0, "avg_ms": 0}


def _int_ms(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _add_latency(dst: dict, duration_ms: int) -> None:
    dst["count"] += 1
    dst["sum_ms"] += duration_ms
    dst["max_ms"] = max(dst["max_ms"], duration_ms)


def _finalize_latency_bucket(bucket: dict) -> dict:
    # avg_ms 為衍生值：收尾一次算（sum // count），count=0 維持 0。
    if bucket["count"]:
        bucket["avg_ms"] = bucket["sum_ms"] // bucket["count"]
    return bucket


def _derive_latency(events: list[dict]) -> dict:
    """從 token_usage 事件的 duration_ms 彙總 provider/model/role 維度的 wall-clock 時延。

    只計 payload 帶 `duration_ms` 的事件（缺欄位＝舊事件，直接跳過），故 count 與
    token_usage 的 calls 是獨立欄位、混入無 duration 的舊事件不失真。負值/非數值由
    _int_ms 防禦（截為 0 或跳過），不讓單一壞事件炸掉 finish_session。每桶存
    {count, sum_ms, max_ms, avg_ms}：sum_ms/count 為權威、avg_ms 衍生，故 /api/metrics
    跨場合併時可安全相加 sum 與 count 再重導 avg（不會有「平均 p99」式失真）。
    """
    total = _blank_latency()
    by_provider: dict[str, dict] = {}
    by_model: dict[str, dict] = {}
    by_role: dict[str, dict] = {}
    for ev in events:
        if ev.get("type") != "token_usage":
            continue
        p = ev.get("payload") or {}
        if "duration_ms" not in p:
            continue
        duration_ms = _int_ms(p.get("duration_ms"))
        provider = str(p.get("provider") or "unknown")
        model = str(p.get("model") or "unknown")
        role = str(p.get("speaker") or "unknown")
        for bucket in (
            total,
            by_provider.setdefault(provider, _blank_latency()),
            by_model.setdefault(model, _blank_latency()),
            by_role.setdefault(role, _blank_latency()),
        ):
            _add_latency(bucket, duration_ms)
    for bucket in (total, *by_provider.values(), *by_model.values(), *by_role.values()):
        _finalize_latency_bucket(bucket)
    return {
        "total": total,
        "by_provider": by_provider,
        "by_model": by_model,
        "by_role": by_role,
    }


def _derive_parallel(events: list[dict]) -> dict:
    """從 done 事件取出並行可觀測性摘要（無則回空 dict）。"""
    for ev in reversed(events):
        if ev.get("type") == "done":
            p = ev.get("payload", {}).get("parallel")
            return p if isinstance(p, dict) else {}
    return {}


# meta 檔快取：絕對路徑 -> (mtime_ns, size, meta)。所有 meta 寫入都走 _write_meta →
# secure_write_root（tmp+rename 必刷 mtime），故 (mtime_ns, size) 雙鍵足以判定失效；
# 以絕對路徑為 key，測試切換 HISTORY_ROOT（tmp_path）天然隔離、互不污染。
_meta_cache: dict[str, tuple[int, int, dict]] = {}


def _reset_meta_cache() -> None:
    """清空 meta 快取（測試兜底用）。"""
    _meta_cache.clear()


def _read_meta_file(path: Path) -> dict | None:
    """讀單一 meta 檔；壞檔回 None（獨立成函式供快取測試 spy）。"""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def list_sessions() -> list[dict]:
    """回傳所有 session 的 meta，依開始時間新到舊。

    每檔以 (mtime_ns, size) 快取，未變動不重讀 JSON；回傳的 meta dict 為快取共享物件，
    呼叫端不得就地修改（需要改動請自行 copy）。
    """
    root = config.HISTORY_ROOT
    if not root.exists():
        return []
    metas: list[dict] = []
    seen: set[str] = set()
    for p in root.glob("*.meta.json"):
        key = str(p)
        try:
            st = p.stat()
        except OSError:  # glob 到 stat 前被刪：跳過
            continue
        stamp = (st.st_mtime_ns, st.st_size)
        cached = _meta_cache.get(key)
        if cached is not None and (cached[0], cached[1]) == stamp:
            meta = cached[2]
        else:
            meta = _read_meta_file(p)
            if meta is None:
                # 壞檔不入快取（避免快取成殭屍），下次仍會重試
                _meta_cache.pop(key, None)
                continue
            _meta_cache[key] = (stamp[0], stamp[1], meta)
        seen.add(key)
        metas.append(meta)
    # 修剪：本輪沒見到的檔（已刪除或壞檔）逐出，防快取無限增長；prefix 帶分隔符
    # 避免誤傷同前綴的兄弟目錄（如 .../history 與 .../history2）
    prefix = str(root) + os.sep
    for stale in [k for k in _meta_cache if k.startswith(prefix) and k not in seen]:
        _meta_cache.pop(stale, None)
    metas.sort(key=lambda m: m.get("started_at", 0), reverse=True)
    return metas


def aggregate_scorecard(sessions: list[dict]) -> dict:
    """跨 session 聚合成果記分卡：成功率、平均輪數、一次過率、退回原因，與近期趨勢。

    趨勢取「最近 10 場 vs 再前 10 場」（sessions 已新→舊排序）——這是『工作室有沒有
    越做越進步』的直接量測：成功率升、平均輪數降＝在進步。
    （自 routes.py 平移至此：history 是 scorecard 推導 SSOT，改良迴圈也要複用聚合。）
    """
    rows = [
        (m, m["scorecard"])
        for m in sessions
        if m.get("status") != "running" and isinstance(m.get("scorecard"), dict)
    ]
    if not rows:
        return {
            "n": 0,
            "qa_pass_rate": None,
            "critic_pass_rate": None,
            "demo_pass_rate": None,
        }

    def _slice_stats(part: list[tuple[dict, dict]]) -> dict:
        if not part:
            return {"n": 0}
        done = sum(1 for m, _ in part if m.get("status") == "completed")
        rounds = [s["avg_rounds"] for _, s in part if s.get("avg_rounds")]
        return {
            "n": len(part),
            "completed_rate": round(done / len(part), 2),
            "avg_rounds": round(sum(rounds) / len(rounds), 2) if rounds else None,
        }

    rejects = {"qa_fail": 0, "smoke_fail": 0, "gate_veto": 0, "critic": 0, "stall": 0}
    tasks_total = tasks_done = first_try = 0
    qa_total = qa_pass = 0
    critic_total = critic_pass = 0
    demo_total = demo_pass = 0
    for _, s in rows:
        for k in rejects:
            rejects[k] += (s.get("rejects") or {}).get(k, 0)
        tasks_total += s.get("tasks_total", 0)
        tasks_done += s.get("tasks_done", 0)
        first_try += s.get("first_try_done", 0)
        qa_total += s.get("qa_total", 0)
        qa_pass += s.get("qa_pass", 0)
        critic_total += s.get("critic_total", 0)
        critic_pass += s.get("critic_pass", 0)
        demo = s.get("demo_passed")
        if demo is not None:
            demo_total += 1
            if demo is True:
                demo_pass += 1

    def _rate(passed: int, total: int) -> float | None:
        return round(passed / total, 2) if total else None

    return {
        **_slice_stats(rows),
        "qa_pass_rate": _rate(qa_pass, qa_total),
        "critic_pass_rate": _rate(critic_pass, critic_total),
        "demo_pass_rate": _rate(demo_pass, demo_total),
        "tasks": {
            "total": tasks_total,
            "done": tasks_done,
            "first_try_done": first_try,
            "first_try_rate": round(first_try / tasks_done, 2) if tasks_done else None,
        },
        "rejects": rejects,
        "trend": {"recent": _slice_stats(rows[:10]), "previous": _slice_stats(rows[10:20])},
    }


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


def events_mtime(session_id: str) -> float | None:
    """events 檔 mtime（無檔或讀不到回 None）——代表 session 的最後活動時間。

    供 autopilot 任務中心跳把 last_activity_at 寫進 status.json（外部監控據此
    分辨「長任務仍在動」與「真的卡死」）。
    """
    try:
        return _events_path(session_id).stat().st_mtime
    except OSError:
        return None


def sweep_stale_running(
    active_sids: frozenset[str] | set[str] = frozenset(), stale_after_s: float | None = None
) -> list[str]:
    """掃除卡在 running 的幽靈 meta：非活躍且久無活動者標 error（mark_interrupted），回傳掃到的 sid。

    autopilot／服務被 restart 殺掉時 finish_session 沒跑到，meta 永遠停在 running——
    網站無限顯示 ⏳ 執行中、enforce_retention 也永不回收。此函式挑出「sid 不在
    active_sids（呼叫端提供的活躍集合，如 busy_sessions）且最後活動（events 檔 mtime，
    取不到退回 meta 時間戳）超過 stale_after_s 秒」的 running meta 逐一標中斷。

    stale_after_s 預設（None）＝max(3600, 2 × config.TURN_HARD_TIMEOUT)，每次呼叫即時
    計算：安全不變量是「單一專家 turn 依 TURN_HARD_TIMEOUT 可合法靜默」的**兩倍**——
    TI_TURN_TIMEOUT 是執行期可調（config.reload）的設定，門檻寫死 3600 會在 turn
    timeout 調大後誤殺「討論很長但活著」的場次；3600 為下限地板（預設 1800×2）。
    mark_interrupted 冪等（只動 running），重複掃無副作用。
    """
    if stale_after_s is None:
        stale_after_s = max(3600.0, 2 * float(config.TURN_HARD_TIMEOUT or 0))
    now = time.time()
    swept: list[str] = []
    for meta in list_sessions():
        if meta.get("status") != "running":
            continue
        sid = meta.get("session_id") or ""
        if not sid or sid in active_sids:
            continue
        if now - _last_activity_ts(meta) <= stale_after_s:
            continue
        note = f"stale-running 掃除：無活躍程序且超過 {int(stale_after_s)}s 無活動，標記中斷"
        if mark_interrupted(sid, note):
            swept.append(sid)
    if swept:
        log.info("stale-running 掃除 %d 個幽靈 session：%s", len(swept), "、".join(swept))
    return swept


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
    # 只需計數：走 iter_events 逐筆數，不物化整個 list（長 session O(1) 記憶體）。
    meta["n_events"] = sum(1 for _ in iter_events(session_id))
    _write_meta(session_id, meta)
    return True


def iter_events(session_id: str) -> Iterator[dict]:
    """逐筆疊代 session 事件（惰性讀檔，不一次載入全檔）。

    語義與 load_events 完全一致：檔案不存在→空、空行/壞 JSON 行跳過。
    只需計數或串流掃描時用本函式（O(1) 記憶體）；需要整個 list 用 load_events。
    """
    path = _events_path(session_id)
    if not path.is_file():
        return
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def load_events(session_id: str) -> list[dict]:
    return list(iter_events(session_id))


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
