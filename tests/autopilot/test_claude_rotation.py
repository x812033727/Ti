"""Claude 訂閱雙帳號負載平衡：_maybe_rotate_claude_account 的防護與副作用、主迴圈接線。

決策純函式 pick_account（v4 優先序：95% 安全上限 > 7d 早重置多吃 > 5h 早重置多吃 >
負載平均分配＋margin 遲滯）的規則矩陣在 tests/test_claude_accounts.py；本檔只驗 autopilot
執行點的合約：claude 訂閱模式才輪替、兩窗（5h/7d）用量與 reset_at 正確餵進決策、有討論
進行中不切、switch → 排程重啟的呼叫順序、重複排程防護（180s 窗內不排第二次）、切換失敗
不炸主迴圈、主迴圈在 gate 睡眠判斷「之前」輪替並寫 rotate_restart 心跳（含 rotated_to）。
全部合成快照＋monkeypatch，不打外網、不動真憑證、不起真重啟。
"""

from __future__ import annotations

import asyncio as real_asyncio
import json
import time
import types

import pytest

from studio import autopilot, config

# --- 合成快照 helper（與 tests/autopilot/test_quota_gate.py 同構）-----------


def _acct(
    label: str,
    used: float | None,
    *,
    active: bool = False,
    error: str | None = None,
    fhr: float | None = None,
    sdr: float | None = None,
):
    """單一帳號條目：used 取 5h 窗（7d 窗放較低值，驗證取 max 的語意）；可指定兩窗 reset_at。"""
    rl: dict = {"error": error}
    if used is not None:
        rl["five_hour"] = {
            "used_percentage": used,
            "reset_at": fhr if fhr is not None else time.time() + 3600,
        }
        rl["seven_day"] = {
            "used_percentage": max(0.0, used - 5),
            "reset_at": sdr if sdr is not None else time.time() + 86400,
        }
    return {"label": label, "subscription": "max", "active": active, "rate_limits": rl}


def _snap(accounts: list[dict], *, active_used: float | None = None, extra: list | None = None):
    """claude 區塊快照：頂層 rate_limits 為在線帳號用量（gate 只看得到這個）。"""
    rl = None
    if active_used is not None:
        rl = {
            "five_hour": {"used_percentage": active_used, "reset_at": time.time() + 3600},
            "error": None,
        }
    providers = [{"key": "claude", "ready": True, "rate_limits": rl, "accounts": accounts}]
    return {"ok": True, "updated_at": time.time(), "providers": providers + (extra or [])}


@pytest.fixture
def rotate_env(monkeypatch):
    """claude 訂閱模式＋輪替開啟＋預設無討論進行中；回傳 switch/restart 呼叫紀錄。"""
    calls: list[tuple] = []
    monkeypatch.setattr(config, "CLAUDE_ROTATE", True)
    monkeypatch.setattr(config, "CLAUDE_ACCOUNT_PREFERRED", "B")
    monkeypatch.setattr(config, "CLAUDE_ROTATE_THRESHOLD", 95.0)
    monkeypatch.setattr(config, "CLAUDE_ROTATE_MARGIN", 10.0)
    monkeypatch.setattr(config, "CLAUDE_ROTATE_RESET_EDGE", 900.0)
    monkeypatch.setattr(config, "CLAUDE_ROTATE_RESET_EDGE_7D", 21600.0)
    monkeypatch.setattr(config, "PROVIDER", "claude")
    monkeypatch.setattr(config, "has_api_key", lambda: False)
    monkeypatch.setattr(config, "claude_cli_logged_in", lambda: True)
    # 重複排程防護的模組級旗標歸零，避免測試間洩漏（正式行程由重啟自然歸零）。
    monkeypatch.setattr(autopilot, "_rotate_scheduled_at", None)
    monkeypatch.setattr(autopilot.history, "busy_sessions", lambda _stale: [])
    monkeypatch.setattr(
        autopilot.claude_accounts, "switch", lambda label: calls.append(("switch", label))
    )
    monkeypatch.setattr(
        autopilot.deploy, "schedule_service_restart", lambda: calls.append(("restart",))
    )
    return calls


# --- helper：切換副作用與呼叫順序 -------------------------------------------


def test_rotate_switches_then_schedules_restart_in_order(rotate_env):
    """在線 B 達安全上限、A 低 → 先 switch("A") 再排程重啟（順序不可反）。"""
    snap = _snap([_acct("A", 10.0), _acct("B", 96.0, active=True)])
    assert autopilot._maybe_rotate_claude_account(snap) == "A"
    assert rotate_env == [("switch", "A"), ("restart",)]


def test_rotate_balances_on_margin_gap_with_log(rotate_env, caplog):
    """平均分配案（兩窗重置差皆 <edge）：B 負載 40、A 18（差 ≥margin）→ 切 A；log 為「帳號分配」樣式。"""
    t0 = time.time() + 3600
    sdr = t0 + 86400  # 兩帳號 7d 重置相同 → 7d 規則不觸發（差 0 <edge_7d）
    snap = _snap(
        [_acct("A", 18.0, fhr=t0, sdr=sdr), _acct("B", 40.0, active=True, fhr=t0, sdr=sdr)]
    )
    with caplog.at_level("INFO", logger="ti.autopilot"):
        assert autopilot._maybe_rotate_claude_account(snap) == "A"
    assert rotate_env == [("switch", "A"), ("restart",)]
    hhmm = time.strftime("%H:%M", time.localtime(t0))
    md_hm = time.strftime("%m/%d %H:%M", time.localtime(sdr))
    assert (
        f"Claude 帳號分配：切至 A（5h 重置 {hhmm}；7d 重置 {md_hm}；負載 A 18/B 40）" in caplog.text
    )


def test_rotate_prefers_earlier_reset_with_log(rotate_env, caplog):
    """5h 重置優先案：A 比在線 B 早 42 分（≥edge）→ 即使 A 負載較高也切；log 含「較 B 早 42 分」。"""
    ra = time.time() + 3600
    rt = ra - 42 * 60
    sdr = ra + 86400  # 兩帳號 7d 重置相同 → 7d 規則不觸發，由 5h 規則決策
    snap = _snap(
        [_acct("A", 30.0, fhr=rt, sdr=sdr), _acct("B", 26.0, active=True, fhr=ra, sdr=sdr)]
    )
    with caplog.at_level("INFO", logger="ti.autopilot"):
        assert autopilot._maybe_rotate_claude_account(snap) == "A"
    assert rotate_env == [("switch", "A"), ("restart",)]
    hhmm = time.strftime("%H:%M", time.localtime(rt))
    md_hm = time.strftime("%m/%d %H:%M", time.localtime(sdr))
    assert (
        f"切至 A（5h 重置 {hhmm}，較 B 早 42 分；7d 重置 {md_hm}；負載 A 30/B 26）" in caplog.text
    )


def test_rotate_prefers_7d_earlier_reset(rotate_env, caplog):
    """7d 優先案（2026-07-04 晨間實案縮影）：A 5h 較早（5h 規則會留 A），但 B 的 7d 早
    ~123h ≥edge_7d → 在線 A 應切到 B；log 含 B 的 7d 重置時間。"""
    now = time.time()
    b_sdr = now + 40.7 * 3600
    snap = _snap(
        [
            _acct("A", 53.0, active=True, fhr=now + 2.7 * 3600, sdr=now + 163.7 * 3600),
            _acct("B", 15.0, fhr=now + 3.9 * 3600, sdr=b_sdr),
        ]
    )
    with caplog.at_level("INFO", logger="ti.autopilot"):
        assert autopilot._maybe_rotate_claude_account(snap) == "B"
    assert rotate_env == [("switch", "B"), ("restart",)]
    assert f"7d 重置 {time.strftime('%m/%d %H:%M', time.localtime(b_sdr))}" in caplog.text


def test_rotate_stays_when_active_has_earlier_7d(rotate_env):
    """在線即 7d 最早重置者 → 留在線多吃，不因 5h/負載規則亂切。"""
    now = time.time()
    snap = _snap(
        [
            _acct("A", 53.0, fhr=now + 2.7 * 3600, sdr=now + 163.7 * 3600),
            _acct("B", 15.0, active=True, fhr=now + 3.9 * 3600, sdr=now + 40.7 * 3600),
        ]
    )
    assert autopilot._maybe_rotate_claude_account(snap) is None
    assert rotate_env == []


def test_rotate_reschedule_guard_blocks_double_schedule(rotate_env, monkeypatch):
    """重複排程防護：已排程重啟未滿 180s → 第二次呼叫不再 switch/排程（曾 30 秒排兩次）。"""
    snap = _snap([_acct("A", 10.0), _acct("B", 96.0, active=True)])
    assert autopilot._maybe_rotate_claude_account(snap) == "A"
    assert autopilot._maybe_rotate_claude_account(snap) is None  # 30 秒內第二次 → 擋下
    assert rotate_env == [("switch", "A"), ("restart",)]  # 只切/排程一次


def test_rotate_reschedule_guard_expires(rotate_env, monkeypatch):
    """防護窗過期（>180s）後恢復正常輪替（非 systemd 環境重啟不會來，不得永久卡死）。"""
    monkeypatch.setattr(
        autopilot, "_rotate_scheduled_at", time.time() - autopilot._ROTATE_RESCHEDULE_GUARD_S - 1
    )
    snap = _snap([_acct("A", 10.0), _acct("B", 96.0, active=True)])
    assert autopilot._maybe_rotate_claude_account(snap) == "A"
    assert rotate_env == [("switch", "A"), ("restart",)]


def test_rotate_reset_gap_below_edge_uses_load_rule(rotate_env):
    """重置差 <edge → 退回負載規則：負載差 5 <margin → 不切（重置差不足不觸發）。"""
    t0 = time.time()
    snap = _snap([_acct("A", 35.0, fhr=t0 + 600), _acct("B", 40.0, active=True, fhr=t0 + 1200)])
    assert autopilot._maybe_rotate_claude_account(snap) is None
    assert rotate_env == []


def test_claude_accounts_usage_extracts_windows_and_resets(rotate_env):
    """_claude_accounts_usage：兩窗用量與 reset_at 一起抽；error 帳號全欄位 None。"""
    t0 = time.time()
    snap = _snap(
        [
            _acct("A", 18.0, fhr=t0 + 600, sdr=t0 + 86400),
            _acct("B", None, active=True, error="stale_label"),
        ]
    )
    usages, active = autopilot._claude_accounts_usage(snap)
    assert active == "B"
    assert usages["A"]["five_hour"] == 18.0 and usages["A"]["seven_day"] == 13.0
    assert usages["A"]["five_hour_reset"] == pytest.approx(t0 + 600)
    assert usages["A"]["seven_day_reset"] == pytest.approx(t0 + 86400)
    assert usages["B"] == {
        "five_hour": None,
        "seven_day": None,
        "five_hour_reset": None,
        "seven_day_reset": None,
    }


def test_rotate_gap_below_margin_stays(rotate_env):
    """遲滯：兩帳號負載差 <margin → 不切（避免頻繁重啟）。"""
    snap = _snap([_acct("A", 35.0), _acct("B", 40.0, active=True)])
    assert autopilot._maybe_rotate_claude_account(snap) is None
    assert rotate_env == []


def test_rotate_returns_to_preferred_after_reset(rotate_env):
    """在線 A 負載 50、preferred B 已重置（負載 3，差 ≥margin）→ 平衡切回 B。"""
    snap = _snap([_acct("A", 50.0, active=True), _acct("B", 3.0)])
    assert autopilot._maybe_rotate_claude_account(snap) == "B"
    assert rotate_env == [("switch", "B"), ("restart",)]


def test_rotate_busy_discussion_defers(rotate_env, monkeypatch):
    """有「真正進行中」的討論 → 本輪不切（鏡射 ti-autodeploy 的 busy 判定）。"""
    monkeypatch.setattr(autopilot.history, "busy_sessions", lambda _stale: [{"session_id": "s1"}])
    snap = _snap([_acct("A", 10.0), _acct("B", 96.0, active=True)])
    assert autopilot._maybe_rotate_claude_account(snap) is None
    assert rotate_env == []


def test_rotate_disabled_noop(rotate_env, monkeypatch):
    """CLAUDE_ROTATE=0 → 完全不動作。"""
    monkeypatch.setattr(config, "CLAUDE_ROTATE", False)
    snap = _snap([_acct("A", 10.0), _acct("B", 96.0, active=True)])
    assert autopilot._maybe_rotate_claude_account(snap) is None
    assert rotate_env == []


@pytest.mark.parametrize(
    "attr,value",
    [
        ("PROVIDER", "codex"),  # provider 非 claude
        ("has_api_key", lambda: True),  # 走 API key、非訂閱
        ("claude_cli_logged_in", lambda: False),  # CLI 未登入
    ],
)
def test_rotate_non_claude_subscription_noop(rotate_env, monkeypatch, attr, value):
    """非「claude 訂閱模式」→ 直接 return，不查帳號也不切換。"""
    monkeypatch.setattr(config, attr, value)
    snap = _snap([_acct("A", 10.0), _acct("B", 96.0, active=True)])
    assert autopilot._maybe_rotate_claude_account(snap) is None
    assert rotate_env == []


def test_rotate_all_exhausted_noop(rotate_env):
    """兩邊負載都 ≥95%（安全上限）→ 不切換（交給既有 quota gate 睡到重置）。"""
    snap = _snap([_acct("A", 97.0), _acct("B", 95.0, active=True)])
    assert autopilot._maybe_rotate_claude_account(snap) is None
    assert rotate_env == []


def test_rotate_error_account_not_a_target(rotate_env):
    """另一帳號額度查詢異常（error → 兩窗皆 None）→ 不可切入、不動作。"""
    snap = _snap([_acct("A", None, error="stale_label"), _acct("B", 96.0, active=True)])
    assert autopilot._maybe_rotate_claude_account(snap) is None
    assert rotate_env == []


def test_rotate_switch_failure_does_not_raise(rotate_env, monkeypatch):
    """switch 炸掉 → 只留 log 回 None，不排程重啟、不往外拋（主迴圈安全）。"""

    def boom(_label):
        raise ValueError("找不到帳號的憑證檔")

    monkeypatch.setattr(autopilot.claude_accounts, "switch", boom)
    snap = _snap([_acct("A", 10.0), _acct("B", 96.0, active=True)])
    assert autopilot._maybe_rotate_claude_account(snap) is None
    assert rotate_env == []  # restart 不得被呼叫


# --- config：新設定進 config.py + reload() ----------------------------------


def test_config_rotate_defaults():
    assert config.CLAUDE_ROTATE is True
    assert config.CLAUDE_ACCOUNT_PREFERRED == "B"
    assert config.CLAUDE_ROTATE_THRESHOLD == 95.0  # 安全上限
    assert config.CLAUDE_ROTATE_MARGIN == 10.0  # 平均分配遲滯
    assert config.CLAUDE_ROTATE_RESET_EDGE == 900.0  # 5h 早重置優先的最小差距（秒）
    assert config.CLAUDE_ROTATE_RESET_EDGE_7D == 21600.0  # 7d 早重置優先的最小差距（秒，6h）


def test_config_reload_reads_rotate_env(monkeypatch):
    monkeypatch.setenv("TI_CLAUDE_ROTATE", "0")
    monkeypatch.setenv("TI_CLAUDE_ACCOUNT_PREFERRED", "A")
    monkeypatch.setenv("TI_CLAUDE_ROTATE_THRESHOLD", "80")
    monkeypatch.setenv("TI_CLAUDE_ROTATE_MARGIN", "5")
    monkeypatch.setenv("TI_CLAUDE_ROTATE_RESET_EDGE", "300")
    monkeypatch.setenv("TI_CLAUDE_ROTATE_RESET_EDGE_7D", "7200")
    try:
        config.reload()
        assert config.CLAUDE_ROTATE is False
        assert config.CLAUDE_ACCOUNT_PREFERRED == "A"
        assert config.CLAUDE_ROTATE_THRESHOLD == 80.0
        assert config.CLAUDE_ROTATE_MARGIN == 5.0
        assert config.CLAUDE_ROTATE_RESET_EDGE == 300.0
        assert config.CLAUDE_ROTATE_RESET_EDGE_7D == 7200.0
    finally:
        monkeypatch.undo()
        config.reload()


# --- 主迴圈接線（與 test_quota_gate.py 同構的 stub）-------------------------


class _Stop(BaseException):
    """跳出 main() 無限迴圈用；繼承 BaseException 以免被 except Exception 吃掉。"""


@pytest.fixture
def state_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "AUTOPILOT_QUOTA_GATE", True)
    monkeypatch.setattr(config, "AUTOPILOT_QUOTA_MAX_SLEEP", 1800)
    monkeypatch.setattr(config, "AUTOPILOT_STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(config, "autopilot_paused", lambda: False)
    return tmp_path / "state"


def _read_status(state_dir):
    return json.loads((state_dir / "status.json").read_text(encoding="utf-8"))


def _stub_asyncio(monkeypatch, sleeps):
    """把 autopilot 模組內的 asyncio 換成 stub：sleep 記錄秒數後丟 _Stop 跳出迴圈。"""

    async def fake_sleep(s):
        sleeps.append(s)
        raise _Stop

    monkeypatch.setattr(
        autopilot,
        "asyncio",
        types.SimpleNamespace(to_thread=real_asyncio.to_thread, sleep=fake_sleep),
    )


async def test_main_loop_rotates_before_gate_sleep(rotate_env, state_dir, monkeypatch):
    """在線帳號受限但另一帳號有額度 → 輪替（而非被 gate 誤判全受限睡到重置），
    本輪不取任務、心跳寫 rotate_restart 且 quota 帶 rotated_to。"""
    # 頂層（在線帳號）用量 96%：若輪替不在 gate 睡眠判斷之前，claude-only 會直接 quota_sleep。
    snap = _snap([_acct("A", 10.0), _acct("B", 96.0, active=True)], active_used=96.0)
    monkeypatch.setattr(autopilot.provider_quota, "snapshot", lambda: snap)
    monkeypatch.setattr(
        autopilot.backlog, "next_pending", lambda: pytest.fail("輪替後本輪不得取任務")
    )
    sleeps: list[float] = []
    _stub_asyncio(monkeypatch, sleeps)

    with pytest.raises(_Stop):
        await autopilot.main()

    assert rotate_env == [("switch", "A"), ("restart",)]
    assert sleeps == [autopilot._ROTATE_RESTART_SLEEP]  # 等 systemd 重啟接手的短睡眠
    status = _read_status(state_dir)
    assert status["state"] == "rotate_restart"
    assert status["quota"]["rotated_to"] == "A"
    assert status["quota"]["claude"] == 96.0
    assert status["sleep_until"] == pytest.approx(
        time.time() + autopilot._ROTATE_RESTART_SLEEP, abs=30
    )


async def test_main_loop_no_rotation_takes_task(rotate_env, state_dir, monkeypatch):
    """不需切換（負載差 <margin 且未達安全上限）→ 照常取任務跑，心跳寫 running。"""
    snap = _snap([_acct("A", 45.0), _acct("B", 50.0, active=True)], active_used=50.0)
    monkeypatch.setattr(autopilot.provider_quota, "snapshot", lambda: snap)
    monkeypatch.setattr(autopilot, "_recover_stale_in_progress", lambda: None)
    monkeypatch.setattr(autopilot.backlog, "next_pending", lambda: {"id": 9, "title": "t"})

    async def fake_run(_task):
        raise _Stop

    monkeypatch.setattr(autopilot, "run_one_task", fake_run)
    with pytest.raises(_Stop):
        await autopilot.main()

    assert rotate_env == []
    status = _read_status(state_dir)
    assert status["state"] == "running" and status["task_id"] == 9
    assert "rotated_to" not in status["quota"]


async def test_main_loop_rotate_disabled_falls_through(rotate_env, state_dir, monkeypatch):
    """CLAUDE_ROTATE=0：即便 B 達安全上限也不切換，主迴圈走既有 gate/取任務路徑。"""
    monkeypatch.setattr(config, "CLAUDE_ROTATE", False)
    # 另附一個可用 provider，讓 gate 判定 usable → 直接取任務（不進 quota_sleep）。
    extra = [{"key": "codex", "ready": True, "rate_limits": {"five_hour": {"used_percentage": 5}}}]
    snap = _snap([_acct("A", 10.0), _acct("B", 96.0, active=True)], active_used=96.0, extra=extra)
    monkeypatch.setattr(autopilot.provider_quota, "snapshot", lambda: snap)
    monkeypatch.setattr(autopilot, "_recover_stale_in_progress", lambda: None)
    taken: list[dict] = []

    def next_pending():
        task = {"id": 3, "title": "t"}
        taken.append(task)
        return task

    async def fake_run(_task):
        raise _Stop

    monkeypatch.setattr(autopilot.backlog, "next_pending", next_pending)
    monkeypatch.setattr(autopilot, "run_one_task", fake_run)
    with pytest.raises(_Stop):
        await autopilot.main()

    assert rotate_env == []  # 不得 switch/restart
    assert [t["id"] for t in taken] == [3]


async def test_main_loop_busy_discussion_no_rotation(rotate_env, state_dir, monkeypatch):
    """有討論進行中 → 不切換；claude-only 且在線帳號受限時交給 gate 睡眠（既有行為）。"""
    monkeypatch.setattr(autopilot.history, "busy_sessions", lambda _stale: [{"session_id": "s1"}])
    snap = _snap([_acct("A", 10.0), _acct("B", 96.0, active=True)], active_used=96.0)
    monkeypatch.setattr(autopilot.provider_quota, "snapshot", lambda: snap)
    sleeps: list[float] = []
    _stub_asyncio(monkeypatch, sleeps)

    with pytest.raises(_Stop):
        await autopilot.main()

    assert rotate_env == []
    assert _read_status(state_dir)["state"] == "quota_sleep"
