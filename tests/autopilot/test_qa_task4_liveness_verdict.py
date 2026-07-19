"""守門測試 #4：判死規則 **repo 內正典實作** `autopilot.liveness_verdict` 的黑白判別。

範圍誠實聲明（回應 critic 異議）：真正執行 restart 的是 repo 外的「層3監控」腳本，它並不
import `liveness_verdict`。本檔鎖的是**函式這份參照實作**的 AND 邏輯不被悄悄放寬——它**不能**
保證外部監控同步，那類回歸須靠外部監控自身的測試。此函式被文件（`autopilot-monitoring.md`
「repo 內正典實作」節）標為對齊基準，並由 `tests/docs/test_qa_task4_liveness_ssot_doc.py`
防文件與函式漂移；三處合起來把「散文規則→可執行正典→外部對齊要求」串成可查證的鏈，而非
假稱「共用同一份程式」。

對照 2026-07-04 誤殺（issue #285）：長輪多專家討論的 inter-message 間隔期間 events mtime／
`last_activity_at` 凍結 30–90 分鐘，但子行程仍在燒 CPU。若監控只看 `last_activity_at` 就會
把健康長 turn 誤判死鎖並 restart，丟失數小時進度。本檔鎖住 `docs/guides/autopilot-monitoring.md`
規則 1–5 的 AND 邏輯不被放寬。

真判別（非套套邏輯）由「同一情境翻轉單一欄位即翻轉判定」證明：
  白樣本 cpu_active=True→alive，抽掉救命訊號改 cpu_active=False→dead_task（見
  ``test_black_flip_cpu_active_false_kills`` 的反向斷言）。

紅樣本實證（交付前手動破壞）：把 ``liveness_verdict`` 規則 2 的
``if cpu_active is True or not activity_stale`` 短路砍成 ``if not activity_stale``（＝忽略
cpu_active 救命訊號、放寬成純 last_activity 判死），跑本檔——
``test_white_long_turn_cpu_active_not_killed`` 立即紅：
``AssertionError: 長 turn cpu_active=True 不得判死: got 'dead_task'``。證明白樣本真的在測
cpu_active 這條 AND 子句，不是恆綠。
"""

from __future__ import annotations

from studio import autopilot

# 門檻取 180s（3× 60s 刷新間隔，對齊 docs「≥3× 刷新間隔」建議）；now 固定，各樣本用相對位移。
NOW = 1_783_140_000.0
THRESH = 180.0
FRESH = NOW - 5.0  # 門檻內＝新鮮
STALE = NOW - 3600.0  # 遠超門檻（凍結約 1 小時）＝長不動


def _running(**over) -> dict:
    """running 狀態的健康基線；各測試只覆寫要驗的欄位。"""
    base = {
        "state": "running",
        "task_id": 42,
        "sleep_until": None,
        "updated_at": FRESH,
        "quota": {"claude": 12},
        "last_activity_at": FRESH,
        "current_expert": "senior",
        "turn_started_at": STALE,  # 專家已跑很久——但不參與判死
        "workers": {"count": 5, "cpu_active": True},
    }
    base.update(over)
    return base


def _verdict(status: dict) -> str:
    return autopilot.liveness_verdict(status, now=NOW, stale_threshold_s=THRESH)


# --- 白樣本：長 turn 無事件但 cpu_active=true → 不判死 -----------------------------


def test_white_long_turn_cpu_active_not_killed():
    """核心白樣本：last_activity_at 凍結約 1 小時，但 cpu_active=True → 不得判死（規則 2）。

    這正是 issue #285 誤報情境：有 5 個 worker 在燒 CPU，長 inter-message 間隔不代表死鎖。
    """
    status = _running(last_activity_at=STALE, workers={"count": 5, "cpu_active": True})
    assert (
        _verdict(status) == "alive"
    ), f"長 turn cpu_active=True 不得判死: got {_verdict(status)!r}"


def test_white_activity_fresh_even_if_cpu_idle():
    """last_activity_at 仍前進（新鮮）→ 即使 cpu_active=False 也 alive（規則 2「或」）。"""
    status = _running(last_activity_at=FRESH, workers={"count": 5, "cpu_active": False})
    assert _verdict(status) == "alive"


def test_white_cpu_none_activity_fresh_not_killed():
    """cpu_active=None（/proc 不可用／首 tick）不可單獨判死；last_activity 新鮮 → alive（規則 4）。"""
    status = _running(last_activity_at=FRESH, workers={"count": None, "cpu_active": None})
    assert _verdict(status) == "alive"


# --- 黑樣本：主迴圈停滯 → 判死 -----------------------------------------------------


def test_black_main_loop_stall_kills():
    """核心黑樣本：updated_at 停滯超過門檻＝主迴圈存活訊號斷了 → dead_main_loop（規則 1）。"""
    status = _running(updated_at=STALE)
    assert _verdict(status) == "dead_main_loop"


def test_black_main_loop_stall_kills_regardless_of_worker_cpu():
    """主迴圈死優先：即使 cpu_active=True（子行程還在跑）updated_at 停滯仍判 dead_main_loop。

    updated_at 是主行程層訊號，worker 燒 CPU 救不了掛掉的主迴圈。
    """
    status = _running(updated_at=STALE, workers={"count": 5, "cpu_active": True})
    assert _verdict(status) == "dead_main_loop"


def test_black_missing_updated_at_kills():
    """舊／壞 status.json 缺 updated_at → 當作停滯，判 dead_main_loop（null-safe，不拋例外）。"""
    status = _running()
    del status["updated_at"]
    assert _verdict(status) == "dead_main_loop"


def test_black_flip_cpu_active_false_kills():
    """AND 兩子句同時成立才殺：last_activity 凍結 且 cpu_active=False → dead_task。

    與 ``test_white_long_turn_cpu_active_not_killed`` 對照——同一凍結情境，唯一差別是
    cpu_active True→False 即翻轉 alive→dead_task，證明白樣本非恆綠、cpu_active 子句真判別。
    """
    status = _running(last_activity_at=STALE, workers={"count": 5, "cpu_active": False})
    assert _verdict(status) == "dead_task"


def test_black_cpu_none_activity_stale_kills():
    """cpu_active=None 且 last_activity 長不動 → 退回純 last_activity 判死 dead_task（規則 4）。"""
    status = _running(last_activity_at=STALE, workers={"count": None, "cpu_active": None})
    assert _verdict(status) == "dead_task"


def test_black_missing_last_activity_and_cpu_false_kills():
    """last_activity_at 缺值（None）視為 stale，配 cpu_active=False → dead_task。"""
    status = _running(workers={"count": 5, "cpu_active": False})
    del status["last_activity_at"]
    assert _verdict(status) == "dead_task"


# --- AND 邏輯不放寬：cpu_active 或 last_activity 任一存活即不殺 -----------------------


def test_and_rule_not_relaxed_matrix():
    """規則 3 AND 真值表：唯有(cpu_active∈{False,None} 且 last_activity 凍結)才殺。"""
    cases = [
        # (cpu_active, activity_stale) -> verdict
        (True, True, "alive"),  # cpu 救命
        (True, False, "alive"),
        (False, False, "alive"),  # last_activity 救命
        (None, False, "alive"),  # 首 tick + 有進展
        (False, True, "dead_task"),  # AND 成立
        (None, True, "dead_task"),  # 退回 last_activity（stale）
    ]
    for cpu_active, stale, expected in cases:
        activity = STALE if stale else FRESH
        status = _running(last_activity_at=activity, workers={"count": 5, "cpu_active": cpu_active})
        got = _verdict(status)
        assert got == expected, f"cpu_active={cpu_active} stale={stale}: 期望 {expected} 得 {got}"


# --- 規則 5：current_expert / turn_started_at 不參與判死 ---------------------------


def test_turn_fields_never_affect_verdict():
    """long-stuck 的 current_expert / 很舊的 turn_started_at 不得改變判定（規則 5）。

    健康長 turn 本就停在同一專家很久——若拿 turn 欄位判死會重演誤殺。
    """
    healthy = _running(last_activity_at=STALE, workers={"count": 5, "cpu_active": True})
    assert _verdict(healthy) == "alive"
    # 換不同專家、turn_started_at 推到更久以前，判定必須不變。
    assert (
        _verdict({**healthy, "current_expert": "qa", "turn_started_at": NOW - 99999.0}) == "alive"
    )
    # turn 欄位缺失（舊 status.json）也不得報錯或改判。
    stripped = {k: v for k, v in healthy.items() if k not in ("current_expert", "turn_started_at")}
    assert _verdict(stripped) == "alive"


# --- 睡眠狀態：不因 updated_at 停滯誤判主迴圈死 -----------------------------------


def test_sleep_state_alive_while_sleeping():
    """quota_sleep 期間主迴圈阻塞在 sleep，updated_at 停滯屬正常；sleep_until 未到期 → alive。"""
    status = {
        "state": "quota_sleep",
        "task_id": None,
        "sleep_until": NOW + 600.0,
        "updated_at": STALE,  # 睡眠期間本就不前進
        "quota": {"claude": 0},
    }
    assert _verdict(status) == "alive"


def test_sleep_state_overrun_kills():
    """睡眠早該醒（sleep_until + 門檻已過）卻沒醒 → 主迴圈疑似卡死，dead_main_loop。"""
    status = {
        "state": "budget_sleep",
        "task_id": None,
        "sleep_until": NOW - THRESH - 10.0,
        "updated_at": STALE,
        "quota": {},
    }
    assert _verdict(status) == "dead_main_loop"


# --- idle / stopped：updated_at 新鮮即存活，不做任務層判死 --------------------------


def test_idle_fresh_is_alive():
    status = {"state": "idle", "updated_at": FRESH, "quota": {"claude": 50}}
    assert _verdict(status) == "alive"


def test_idle_stale_updated_at_is_dead_main_loop():
    status = {"state": "idle", "updated_at": STALE, "quota": {}}
    assert _verdict(status) == "dead_main_loop"
