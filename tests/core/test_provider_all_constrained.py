"""任務 #3：全受限分支的安全 fallback 測試。

需求：``least_constrained_ready`` 回 None 時改為發 ``provider_constrained`` 事件並寫一筆 audit，
**不靜默、不無限 spin**——單次決策即返回，不靠 sleep 迴圈空燒；audit IO 失敗不中斷 session。

涵蓋：
- ``_handle_all_constrained`` 直接單元：事件 payload（role / provider / reason / providers 各家用量）
  + audit row（ts / event / session_id / role / provider / reason / providers）寫入完整。
- ``_quota_audit_providers`` 邊界：bucket 式（antigravity）+ window 式（claude/codex/minimax）、
  snap=None、malformed snap（缺欄位）皆不崩潰。
- 不 spin：實作未引入 ``asyncio.sleep``、無 while/for 迴圈（靜態可 grep）。
- audit IO 容錯：``AUTOPILOT_STATE_DIR`` 已被佔用為普通檔案 → session 不掛、log.warning。
- 多角色受限：每個角色各發一筆事件＋各寫一行 audit（不聚合、不丟失）。
- ``_pick_provider`` 護欄：snap=None 時不觸發全受限處理（不誤判）。
"""

from __future__ import annotations

import inspect
import json
import logging

import pytest

from studio import config, events
from studio.orchestrator import LaneContext, StudioSession
from studio.roles import BY_KEY, Role

# --- 共用 helpers ----------------------------------------------------------


class StubExpert:
    """依需求的最簡 stub。"""

    def __init__(self, role: Role):
        self.role = role
        self.calls = 0

    async def speak(self, prompt, broadcast):
        self.calls += 1
        await broadcast(
            events.expert_message("t", self.role.key, self.role.name, self.role.avatar, "")
        )
        return ""

    async def stop(self):
        pass


def _all_constrained_snapshot():
    """四家 provider 全部受限（含 antigravity bucket 結構）的合成快照。"""
    return {
        "ok": True,
        "updated_at": 1000.0,
        "providers": [
            {
                "key": "claude",
                "ready": True,
                "rate_limits": {
                    "five_hour": {"used_percentage": 100, "reset_at": 1600.0},
                    "error": None,
                },
            },
            {
                "key": "codex",
                "ready": True,
                "rate_limits": {
                    "five_hour": {"used_percentage": 95, "reset_at": 1700.0},
                    "error": None,
                },
            },
            {
                "key": "minimax",
                "ready": True,
                "rate_limits": {
                    "one_day": {"used_percentage": 92, "reset_at": 1800.0},
                    "error": None,
                },
            },
            {
                "key": "antigravity",
                "ready": True,
                "rate_limits": {
                    "buckets": [{"used_percentage": 91, "reset_at": 1900.0}],
                    "error": None,
                },
            },
        ],
    }


def _make_session(bucket):
    """建一個最小 StudioSession（stub experts、cwd=None）。"""

    async def broadcast(ev):
        bucket.append(ev)

    experts = {
        "pm": StubExpert(BY_KEY["pm"]),
        "engineer": StubExpert(BY_KEY["engineer"]),
    }
    s = StudioSession("qa-task3", broadcast, experts=experts, cwd=None)
    s._main_ctx = LaneContext("main", None, experts, None)
    s._requirement = "驗證 task3 全受限 fallback"
    return s


# --- 白樣本：_handle_all_constrained 直接呼叫 --------------------------------


@pytest.mark.asyncio
async def test_handle_all_constrained_emits_event_with_full_payload(tmp_path):
    """白樣本：all-constrained → 事件 payload 含 role/provider/reason/各 provider 用量。"""
    bucket: list[events.StudioEvent] = []
    s = _make_session(bucket)
    snap = _all_constrained_snapshot()
    monkeypatch_state_dir = tmp_path / "ap"
    monkeypatch_state_dir.mkdir()
    original = config.AUTOPILOT_STATE_DIR
    config.AUTOPILOT_STATE_DIR = monkeypatch_state_dir
    try:
        await s._handle_all_constrained("architect", "claude", snap)
    finally:
        config.AUTOPILOT_STATE_DIR = original

    assert len(bucket) == 1
    ev = bucket[0]
    assert ev.type is events.EventType.PROVIDER_CONSTRAINED
    payload = ev.payload
    assert payload["role"] == "architect"
    assert payload["provider"] == "claude"
    assert payload["reason"] == "no_provider_ready"
    # providers 列表＝四家 provider 的即時用量快照（給前端 / 儀表板定位用）
    assert isinstance(payload["providers"], list) and len(payload["providers"]) == 4
    by_key = {p["key"]: p for p in payload["providers"]}
    assert by_key["claude"]["max_used"] == 100 and by_key["codex"]["max_used"] == 95
    assert by_key["minimax"]["max_used"] == 92 and by_key["antigravity"]["max_used"] == 91
    # soonest_reset 反映各家「最早 reset」→ 儀表板能排程顯示
    assert by_key["claude"]["soonest_reset"] == 1600.0
    assert by_key["antigravity"]["soonest_reset"] == 1900.0


@pytest.mark.asyncio
async def test_handle_all_constrained_appends_audit_row(tmp_path):
    """白樣本：audit.jsonl 含 reason + 各 provider 用量，欄位與事件 payload 對齊。"""
    bucket: list[events.StudioEvent] = []
    s = _make_session(bucket)
    snap = _all_constrained_snapshot()
    ap_dir = tmp_path / "ap"
    ap_dir.mkdir()
    original = config.AUTOPILOT_STATE_DIR
    config.AUTOPILOT_STATE_DIR = ap_dir
    try:
        await s._handle_all_constrained("engineer", "codex", snap)
    finally:
        config.AUTOPILOT_STATE_DIR = original

    audit_path = ap_dir / "audit.jsonl"
    assert audit_path.exists()
    rows = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1
    row = rows[0]
    assert row["event"] == "provider_constrained"
    assert row["session_id"] == "qa-task3"
    assert row["role"] == "engineer"
    assert row["provider"] == "codex"
    assert row["reason"] == "no_provider_ready"
    # providers 內容與事件 payload 一致 → P2 儀表板讀 audit 或讀 event 結果相同
    assert len(row["providers"]) == 4
    by_key = {p["key"]: p for p in row["providers"]}
    assert by_key["claude"]["max_used"] == 100
    assert by_key["antigravity"]["max_used"] == 91
    # ts 浮點數時間戳存在（不驗具體值，避免時鐘漂移導致 flaky）
    assert isinstance(row["ts"], float) and row["ts"] > 0


# --- 黑樣本：audit IO 容錯，不中斷 session -----------------------------------


@pytest.mark.asyncio
async def test_audit_io_failure_does_not_crash_session(tmp_path, caplog):
    """黑樣本：AUTOPILOT_STATE_DIR 已被佔用為普通檔案 → mkdir 拋 OSError → session 不掛。"""
    bucket: list[events.StudioEvent] = []
    s = _make_session(bucket)
    snap = _all_constrained_snapshot()
    blocker = tmp_path / "blocker"  # 普通檔案當目錄用 → mkdir 必失敗
    blocker.write_text("佔住")
    original = config.AUTOPILOT_STATE_DIR
    config.AUTOPILOT_STATE_DIR = blocker
    try:
        with caplog.at_level(logging.WARNING, logger="ti.orchestrator"):
            # 不應拋
            await s._handle_all_constrained("pm", "claude", snap)
    finally:
        config.AUTOPILOT_STATE_DIR = original

    # 事件照樣發出（事件廣播與 audit 寫入解耦；寫入失敗不影響事件流）
    assert len(bucket) == 1
    assert bucket[0].type is events.EventType.PROVIDER_CONSTRAINED
    # 並有 WARNING 紀錄 IO 失敗（可觀測、可定位）
    assert any("provider_constrained audit 寫入失敗" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_audit_append_oserror_is_swallowed(tmp_path, monkeypatch):
    """黑樣本：open("a") 直接拋 OSError → 不冒泡上來。"""
    bucket: list[events.StudioEvent] = []
    s = _make_session(bucket)
    snap = _all_constrained_snapshot()
    ap_dir = tmp_path / "ap"
    ap_dir.mkdir()
    original_dir = config.AUTOPILOT_STATE_DIR
    config.AUTOPILOT_STATE_DIR = ap_dir
    try:
        # 把 audit 檔變唯讀 → open("a") 在某些平台可能不會拋，改為 mock open 強制 OSError
        real_open = open

        def boom_open(*a, **kw):
            if a and isinstance(a[0], type(ap_dir)) and str(a[0]).endswith("audit.jsonl"):
                raise OSError("disk full (simulated)")
            return real_open(*a, **kw)

        monkeypatch.setattr("builtins.open", boom_open)
        # 不應拋
        await s._handle_all_constrained("engineer", "claude", snap)
    finally:
        config.AUTOPILOT_STATE_DIR = original_dir

    # 事件仍發出
    assert len(bucket) == 1 and bucket[0].type is events.EventType.PROVIDER_CONSTRAINED


# --- 白樣本：不 spin、不 sleep、單次決策即返回 -------------------------------


def test_handle_all_constrained_is_single_shot_no_spin():
    """靜態合約：_handle_all_constrained 沒有 while/for/sleep——單次決策即返回。"""
    src = inspect.getsource(StudioSession._handle_all_constrained)
    # 任何 sleep / while / for 都不該出現（單次寫入＋單次廣播，不該靠迴圈重試）
    assert "asyncio.sleep" not in src
    assert "while " not in src  # 避開「while True」之類的空轉
    assert "for " not in src.split("def ")[1]  # 函式本體內不該有 for
    # 明確保留兩個關鍵操作各一次
    assert src.count("await self.broadcast(") == 1
    assert src.count('"a", encoding="utf-8"') == 1


def test_pick_provider_does_not_loop_on_all_constrained():
    """_pick_provider 是純同步決策，不該引入任何迴圈／await。"""
    src = inspect.getsource(StudioSession._pick_provider)
    assert "asyncio.sleep" not in src
    assert "while " not in src
    assert "for " not in src.split("def ")[1]


# --- 白樣本：_quota_audit_providers 兩種 rate_limits 結構 ------------------


def test_quota_audit_providers_window_and_bucket():
    """白樣本：claude/codex/minimax 的 window 式與 antigravity 的 bucket 式皆正確抽出。"""
    s = _make_session([])
    snap = _all_constrained_snapshot()
    out = s._quota_audit_providers(snap)
    assert len(out) == 4
    by_key = {p["key"]: p for p in out}
    # window 式：取最大 used_percentage
    assert by_key["claude"]["max_used"] == 100
    assert by_key["codex"]["max_used"] == 95
    assert by_key["minimax"]["max_used"] == 92
    # bucket 式：取最大 bucket used_percentage
    assert by_key["antigravity"]["max_used"] == 91
    # 全部 ready=True / error=None / soonest_reset 取各家最早
    for p in out:
        assert p["ready"] is True
        assert p["error"] is None
    assert by_key["claude"]["soonest_reset"] == 1600.0
    assert by_key["antigravity"]["soonest_reset"] == 1900.0


def test_quota_audit_providers_handles_snap_none_and_empty():
    """黑樣本：snap=None（_refresh_quota_snapshot 失敗時的常見路徑）／snap 缺 providers
    → 不崩潰、回空 list，事件 payload["providers"] 也是空 list 而非 None
    （前端/儀表板據此判斷「無快照資料」而非「欄位不存在」）。"""
    s = _make_session([])
    assert s._quota_audit_providers(None) == []
    assert s._quota_audit_providers({}) == []
    # 真實場景：snapshot 為空 dict、缺 providers 鍵 → 視為「無資料」回空
    assert s._quota_audit_providers({"ok": True, "updated_at": 1.0}) == []


def test_quota_audit_providers_handles_missing_or_null_rate_limits():
    """黑樣本：rate_limits=None 或缺欄位 → 該 provider 仍能列出（max_used=None），
    不影響事件廣播與 audit 寫入（_handle_all_constrained 仍照常完成）。"""
    s = _make_session([])
    snap = {
        "providers": [
            {"key": "claude"},  # 連 rate_limits 鍵都沒有
            {"key": "codex", "rate_limits": None},  # rate_limits=None
            {
                "key": "minimax",
                "rate_limits": {"five_hour": None, "error": None},  # window 欄位=None
            },
            {
                "key": "antigravity",
                "rate_limits": {"buckets": [], "error": None},  # buckets 空 list
            },
        ]
    }
    out = s._quota_audit_providers(snap)
    assert len(out) == 4
    for p in out:
        # 缺/空欄位 → max_used=None（前端能正確標 N/A，不會把 None 顯示成 0%）
        assert p["max_used"] is None
        assert p["soonest_reset"] is None
        # error 欄位為 None（沒 error）
        assert p["error"] is None


# --- 黑樣本：_pick_provider 護欄（snap=None 時不誤觸） --------------------


def test_pick_provider_no_snapshot_keeps_hint_unprocessed():
    """snap=None 時 _pick_provider 退回有效 provider，且**不**設 pending marker
    ——避免「沒查過額度」被當成「全受限」誤發 provider_constrained 事件。"""
    s = _make_session([])
    s._quota_snap = None  # 模擬 _refresh_quota_snapshot 失敗
    role = BY_KEY["engineer"]
    prov = s._pick_provider(role, "codex")
    assert prov == "codex"  # hint 直接用
    # 沒有標記 = 不會走 _handle_all_constrained
    assert "engineer" not in s._provider_constrained_pending


def test_pick_provider_not_constrained_no_pending():
    """受 snapshot 但 provider 未受限 → 不設 pending、不會發事件。"""
    s = _make_session([])
    # claude 未受限、codex 也未受限 → 不觸發全受限路徑
    s._quota_snap = {
        "ok": True,
        "providers": [
            {
                "key": "claude",
                "ready": True,
                "rate_limits": {
                    "five_hour": {"used_percentage": 30, "reset_at": 2000.0},
                    "error": None,
                },
            },
            {
                "key": "codex",
                "ready": True,
                "rate_limits": {
                    "five_hour": {"used_percentage": 20, "reset_at": 2000.0},
                    "error": None,
                },
            },
        ],
    }
    prov = s._pick_provider(BY_KEY["engineer"], "claude")
    assert prov == "claude"
    assert s._provider_constrained_pending == {}


def test_pick_provider_alternative_exists_rebinds():
    """白樣本：有 least_constrained_ready 回傳時自動重綁（不進 pending），
    驗證既有重綁邏輯不被本次 patch 誤改。"""
    s = _make_session([])
    s._quota_snap = {
        "ok": True,
        "providers": [
            {
                "key": "claude",
                "ready": True,
                "rate_limits": {
                    "five_hour": {"used_percentage": 95, "reset_at": 2000.0},
                    "error": None,
                },
            },
            {
                "key": "codex",
                "ready": True,
                "rate_limits": {
                    "five_hour": {"used_percentage": 30, "reset_at": 2000.0},
                    "error": None,
                },
            },
        ],
    }
    # PM 指定 claude（受限）→ 自動重綁到 codex（30%）
    prov = s._pick_provider(BY_KEY["engineer"], "claude")
    assert prov == "codex"
    # 重綁後不應留下 pending（因為有替代方案、不算全受限）
    assert s._provider_constrained_pending == {}


# --- 白樣本：多角色受限 → 每個角色各發一筆事件 ----------------------------


@pytest.mark.asyncio
async def test_multiple_recruits_each_emit_their_own_event(tmp_path):
    """白樣本：兩個角色（architect + devops）都受限 → 招募兩次 → 兩筆事件、兩行 audit。"""
    bucket: list[events.StudioEvent] = []
    s = _make_session(bucket)
    snap = _all_constrained_snapshot()
    ap_dir = tmp_path / "ap"
    ap_dir.mkdir()
    original = config.AUTOPILOT_STATE_DIR
    config.AUTOPILOT_STATE_DIR = ap_dir
    try:
        await s._handle_all_constrained("architect", "claude", snap)
        await s._handle_all_constrained("devops", "codex", snap)
    finally:
        config.AUTOPILOT_STATE_DIR = original

    # 事件：兩筆、各自帶 role/provider
    pc_evs = [e for e in bucket if e.type is events.EventType.PROVIDER_CONSTRAINED]
    assert len(pc_evs) == 2
    assert {(e.payload["role"], e.payload["provider"]) for e in pc_evs} == {
        ("architect", "claude"),
        ("devops", "codex"),
    }
    # audit：兩行、各自帶 role/provider
    rows = [
        json.loads(line)
        for line in (ap_dir / "audit.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert len(rows) == 2
    assert {(r["role"], r["provider"]) for r in rows} == {
        ("architect", "claude"),
        ("devops", "codex"),
    }
    # 每行 audit 各自帶 ts（單調遞增／獨立事件標記）
    assert rows[0]["ts"] <= rows[1]["ts"]


# --- 白樣本：與既有 _recruit 整合路徑 --------------------------------------


@pytest.mark.asyncio
async def test_recruit_invokes_handle_all_constrained_when_no_alt(monkeypatch, tmp_path):
    """白樣本：透過 _recruit 招募受限角色 → 自動觸發 _handle_all_constrained
    （驗證整合接縫不漏：_pick_provider 設的 pending 會在 _recruit 內被消費）。"""
    from studio import provider_quota

    bucket: list[events.StudioEvent] = []
    s = _make_session(bucket)
    monkeypatch.setattr(provider_quota, "snapshot", _all_constrained_snapshot)
    s._recruit_factory = lambda role, cwd, provider: StubExpert(role)
    ap_dir = tmp_path / "ap"
    ap_dir.mkdir()
    monkeypatch.setattr(config, "AUTOPILOT_STATE_DIR", ap_dir)
    # 模擬 _stage_dynamic 的入口 snapshot 刷新
    await s._refresh_quota_snapshot()

    # 直接呼叫 _recruit 一次（庫招募 + claude hint）
    ctx = s._main_ctx
    key = await s._recruit(ctx, BY_KEY["architect"], "claude", "庫招募")
    assert key == "architect"

    pc_evs = [e for e in bucket if e.type is events.EventType.PROVIDER_CONSTRAINED]
    assert len(pc_evs) == 1
    assert pc_evs[0].payload["role"] == "architect"
    assert pc_evs[0].payload["provider"] == "claude"

    rows = [
        json.loads(line)
        for line in (ap_dir / "audit.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert len(rows) == 1
    assert rows[0]["role"] == "architect"
    assert rows[0]["provider"] == "claude"


# --- 黑樣本：events.provider_constrained 工廠 ------------------------------


def test_events_provider_constrained_factory_shape():
    """黑樣本：events.provider_constrained 產出的事件形狀穩定（防重構破壞 payload 契約）。"""
    ev = events.provider_constrained(
        "sid",
        "qa",
        "claude",
        [{"key": "claude", "ready": True, "max_used": 95, "soonest_reset": 1.0, "error": None}],
    )
    assert ev.type is events.EventType.PROVIDER_CONSTRAINED
    assert ev.session_id == "sid"
    assert ev.payload["role"] == "qa"
    assert ev.payload["provider"] == "claude"
    assert ev.payload["reason"] == "no_provider_ready"
    assert isinstance(ev.payload["providers"], list)
