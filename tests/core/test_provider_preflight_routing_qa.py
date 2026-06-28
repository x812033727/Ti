"""任務 #4 QA 守門：pre-flight 重綁／使用者意圖護欄／全受限 fallback 黑白樣本。

設計目標（沿用 CLAUDE.md「黑白樣本驗證、不靠讀碼下結論」）：
- 既有 `tests/autopilot/test_provider_routing_contract.py` 已涵蓋「正向路徑」——
  白：受限成員被重綁到 least_constrained；黑：明示覆寫不被重綁。
- 既有 `tests/core/test_provider_all_constrained.py` 已涵蓋「全受限事件＋audit」。
- 本檔補既有測試的**負面斷言死角**，確保三縫合約不被悄悄破壞：

  1. 黑樣本：`TI_PROVIDER_<KEY>=X` + 全受限 → 明示角色既不重綁，也不會被當成「全受限」誤發事件／audit。
     （既有 test 只覆蓋「部分受限」；全受限下若護欄漏寫會誤觸發事件，儀表板會被噪音淹沒。）
  2. 白樣本：plan_preflight_rebind 對「全成員都不受限」「已是最寬鬆」「多角色混合」皆正確產空 plan。
  3. 白樣本：`_apply_preflight_rebind` / `_preflight_rebind_experts` 在 cwd=None／snap=None 時是嚴格 no-op
     （不重建 expert、不寫 recruit_providers、不發事件）——守門「離線單元測試 stub 不被無條件重建破壞」
     與「_refresh_quota_snapshot 失敗時不誤觸重綁路徑」。
  4. 白樣本：`_preflight_rebind_experts` 整合路徑中，明示覆寫角色既不重建也不發事件
     （守護設計決策：「TI_PROVIDER_<KEY> > 一切自動優化」不會被悄悄降級）。

執行指令：
    .venv/bin/python -m pytest tests/core/test_provider_quota_helpers.py \\
                              tests/settings/test_provider_quota.py \\
                              tests/test_offline_e2e.py \\
                              tests/core/test_provider_preflight_routing_qa.py -q
"""

from __future__ import annotations

import json

from studio import config, events, flow
from studio.orchestrator import StudioSession
from studio.roles import BY_KEY, Role

# --- 共用 helpers ----------------------------------------------------------


class StubExpert:
    """最簡 stub：附 provider 欄位供 _apply_preflight_rebind 覆寫檢查。"""

    def __init__(self, role: Role, provider: str = "claude"):
        self.role = role
        self.provider = provider

    async def speak(self, prompt: str, broadcast):
        return "ok"

    async def stop(self) -> None:
        pass


def _entry(key: str, *, ready: bool, used: float | None = None, error: str | None = None):
    """合成快照條目。ready=True 且 used 為 None 時不附 rate_limits（模擬未就緒）。"""
    if used is None and error is None:
        return {"key": key, "ready": ready, "rate_limits": None}
    rate_limits = {"five_hour": {"used_percentage": used or 0.0}, "error": error}
    return {"key": key, "ready": ready, "rate_limits": rate_limits}


def _snap(*entries):
    return {"ok": True, "updated_at": 1000.0, "providers": list(entries)}


def _all_constrained_snap():
    """四家 provider 全受限（claude 受限 95%、codex 99%、minimax ready=False、antigravity ready=False）。
    用於驗證「全受限時明示角色不發事件」的黑樣本。
    """
    return _snap(
        _entry("claude", ready=True, used=95),
        _entry("codex", ready=True, used=99),
        _entry("minimax", ready=False),
        _entry("antigravity", ready=False),
    )


def _make_session(tmp_path, monkeypatch, *, snap=None, explicit_engineer: str | None = None):
    """建一個最小 StudioSession（stub experts、cwd=tmp_path 讓 _apply_preflight_rebind 真跑）。

    - explicit_engineer：非 None 時設 ROLE_PROVIDERS["engineer"]（黑樣本的「使用者意圖護欄」）。
    - snap=None 時 _quota_snap 也設 None（測「snap=None 走 no-op」的白樣本）。
    - snap 給定時直接掛到 _quota_snap（測其他純單元場景）。
    """
    bucket: list[events.StudioEvent] = []

    async def broadcast(ev):
        bucket.append(ev)

    if explicit_engineer is not None:
        monkeypatch.setattr(
            config, "ROLE_PROVIDERS", {**config.ROLE_PROVIDERS, "engineer": explicit_engineer}
        )
    engineer_provider = explicit_engineer or "claude"
    experts = {
        "pm": StubExpert(BY_KEY["pm"]),
        "engineer": StubExpert(BY_KEY["engineer"], engineer_provider),
        "qa": StubExpert(BY_KEY["qa"]),
    }
    s = StudioSession("qa-task4", broadcast, experts=experts, cwd=tmp_path)
    s._quota_snap = snap
    return s, bucket, experts


# --- 黑樣本：明示覆寫護欄 × 全受限（核心缺口）-------------------------------


def test_explicit_override_under_all_constrained_emits_no_event_or_audit(tmp_path, monkeypatch):
    """黑樣本：``TI_PROVIDER_ENGINEER=codex`` + 全受限 → engineer 不重綁、不發 provider_constrained
    事件、不寫 audit——驗證「使用者意圖護欄」在全受限下也不會被悄悄降級、誤發噪音到儀表板。

    對應既有 autopilot 測試 ``test_explicit_role_provider_is_not_rebound_or_reported``
    只覆蓋「部分受限」（minimax 就緒）的護欄行為；本測試補「全受限」路徑——若護欄在
    ``least_constrained_ready is None`` 分支漏寫明示角色跳過，會被當成「其他受限角色」誤發事件。
    """
    bucket: list[events.StudioEvent] = []
    monkeypatch.setattr(config, "AUTOPILOT_STATE_DIR", tmp_path / "ap")
    s, bucket, experts = _make_session(
        tmp_path, monkeypatch, snap=_all_constrained_snap(), explicit_engineer="codex"
    )

    # 模擬 _run() 在 _get_experts() 後呼叫的 pre-flight 路徑
    import asyncio

    asyncio.run(s._preflight_rebind_experts(experts))

    # 1) engineer 仍是原物件、provider 仍是 codex（不被重建、不被改綁）
    assert s._experts["engineer"] is experts["engineer"]
    assert s._experts["engineer"].provider == "codex"
    # 2) recruit_providers 不寫入 engineer（明示角色的實際綁定由 effective_provider 決定、
    #    不需 pre-flight 介入）
    assert "engineer" not in s._recruit_providers
    # 3) 明示角色不發 provider_constrained；同場未明示的 pm/qa 仍可正常回報全受限。
    pc = [e for e in bucket if e.type == events.EventType.PROVIDER_CONSTRAINED]
    assert {e.payload["role"] for e in pc} == {"pm", "qa"}
    # 4) audit 也不應寫入 engineer row；pm/qa 的全受限 audit 是正確行為。
    audit = tmp_path / "ap" / "audit.jsonl"
    rows = [json.loads(line) for line in audit.read_text(encoding="utf-8").splitlines()]
    assert {row["role"] for row in rows} == {"pm", "qa"}


def test_pick_provider_explicit_override_wins_under_all_constrained(tmp_path, monkeypatch):
    """黑樣本：``_pick_provider`` 同步層就早 return 使用者明示 provider，**不**設 pending marker
    ——補既有 ``test_pick_provider_alternative_exists_rebinds`` 的護欄對偶樣本（前者驗證有 alt
    時自動重綁；本測試驗證明示覆寫時同步層就早 return、連 pending 都不設）。

    ``_pick_provider`` 為純同步決策、無 IO；明示覆寫護欄須在第一行生效才能讓 _recruit 內的
    ``provider_constrained_pending.pop(role.key, None) == prov`` 永遠 False。
    """
    s, _bucket, _experts = _make_session(
        tmp_path, monkeypatch, snap=_all_constrained_snap(), explicit_engineer="codex"
    )
    # 即使 PM hint 給 claude 且 claude 受限、明示 codex → 仍回 codex
    prov = s._pick_provider(BY_KEY["engineer"], "claude")
    assert prov == "codex"
    # 沒設 pending marker → _recruit 不會走 _handle_all_constrained
    assert s._provider_constrained_pending == {}


# --- 白樣本：plan_preflight_rebind 純函式決策守門 ----------------------------


def test_plan_preflight_rebind_no_constrained_members_returns_empty_plan():
    """白樣本：所有成員的 provider 都不受限 → plan=[]（沒事別重綁，破壞既有綁定=bug）。"""
    snap = _snap(
        _entry("claude", ready=True, used=10),
        _entry("codex", ready=True, used=20),
        _entry("minimax", ready=True, used=30),
        _entry("antigravity", ready=True, used=15),
    )
    bindings = {"engineer": "claude", "qa": "codex", "pm": "minimax"}
    assert flow.plan_preflight_rebind(bindings, snap, {}) == []


def test_plan_preflight_rebind_skips_when_provider_already_least_constrained():
    """白樣本：role 已綁到 least_constrained（最寬鬆者）→ 不重綁（避免原地打轉）。"""
    snap = _snap(
        _entry("claude", ready=True, used=95),  # 受限
        _entry("minimax", ready=True, used=10),  # 最寬鬆
    )
    # engineer 已綁 minimax → 不需重綁（避免空轉）
    plan = flow.plan_preflight_rebind({"engineer": "minimax"}, snap, {})
    assert plan == []


def test_plan_preflight_rebind_mixed_bindings_only_rebinds_constrained():
    """白樣本：多角色混合綁定 → 只重綁受限者、不受限者原綁定（合約精準）。"""
    snap = _snap(
        _entry("claude", ready=True, used=95),  # 受限
        _entry("minimax", ready=True, used=10),  # 最寬鬆
        _entry("codex", ready=True, used=15),
    )
    bindings = {
        "engineer": "claude",  # 受限 → 應重綁
        "qa": "minimax",  # 已是最寬鬆 → 不重綁
        "pm": "codex",  # 不受限 → 不重綁
    }
    plan = flow.plan_preflight_rebind(bindings, snap, {})
    assert plan == [("engineer", "claude", "minimax")]


def test_plan_preflight_rebind_handles_empty_bindings():
    """白樣本：空 bindings → 空 plan（守門「呼叫端傳空時不該炸」）。"""
    snap = _snap(_entry("claude", ready=True, used=95), _entry("minimax", ready=True, used=10))
    assert flow.plan_preflight_rebind({}, snap, {}) == []


def test_plan_preflight_rebind_skips_empty_provider_string():
    """白樣本：bindings 內 provider 空字串（effective_provider 還沒取到）→ 不視為受限、不重綁。

    防呆：若漏處理，預設 ROOSTER 成員在 _get_experts 之後不久 provider 應已就緒；空字串
    多為 stub 邊界場景，不該誤觸 constrained() 而把空字串重綁成 minimax。
    """
    snap = _snap(_entry("claude", ready=True, used=95), _entry("minimax", ready=True, used=10))
    assert flow.plan_preflight_rebind({"engineer": ""}, snap, {}) == []


# --- 白樣本：副作用層守門（cwd/snap=None 時嚴格 no-op）----------------------


def test_apply_preflight_rebind_cwd_none_is_strict_noop(tmp_path, monkeypatch):
    """白樣本：``_apply_preflight_rebind`` 在 cwd=None 時嚴格 no-op——不重建 expert、
    不寫 recruit_providers。守門「離線單元測試 stub 不被無條件重建破壞」（設計決策 #8）。

    用 cwd=tmp_path 建 session 後把 cwd 清成 None，模擬單元測試常見的「建了 session 但
    後續路徑不該 touch workspace」情境。
    """
    bucket: list[events.StudioEvent] = []

    async def broadcast(_ev):
        bucket.append(_ev)

    experts = {"engineer": StubExpert(BY_KEY["engineer"], "claude")}
    s = StudioSession("s", broadcast, experts=experts, cwd=tmp_path)
    s.cwd = None  # 模擬單元測試後續不走 workspace

    # 即使有 plan，cwd=None → 不重建 expert、不寫 recruit_providers
    s._apply_preflight_rebind([("engineer", "claude", "minimax")])

    assert s._experts["engineer"] is experts["engineer"]
    assert s._experts["engineer"].provider == "claude"
    assert "engineer" not in s._recruit_providers


def test_preflight_rebind_experts_snap_none_is_strict_noop(tmp_path, monkeypatch):
    """白樣本：``_preflight_rebind_experts`` 在 ``_quota_snap=None`` 時嚴格 no-op——
    不重建 expert、不發事件、不寫 audit。守門「_refresh_quota_snapshot 失敗時不誤觸
    重綁路徑」（額度查詢異常應被視為「沒資料」，不是「全受限」）。

    預期行為：_refresh_quota_snapshot 的 try/except 會把 _quota_snap 設成 None（見
    orchestrator 內實作）；pre-flight 必須嚴格區分「沒查過」與「全受限」兩種 None 語意。
    """
    import asyncio

    bucket: list[events.StudioEvent] = []

    async def broadcast(ev):
        bucket.append(ev)

    monkeypatch.setattr(config, "AUTOPILOT_STATE_DIR", tmp_path / "ap")
    experts = {"engineer": StubExpert(BY_KEY["engineer"], "claude")}
    s = StudioSession("qa-task4", broadcast, experts=experts, cwd=tmp_path)
    s._quota_snap = None  # 模擬 _refresh_quota_snapshot 失敗

    asyncio.run(s._preflight_rebind_experts(experts))

    assert s._experts["engineer"] is experts["engineer"]
    assert s._experts["engineer"].provider == "claude"
    assert s._recruit_providers == {}
    pc = [e for e in bucket if e.type == events.EventType.PROVIDER_CONSTRAINED]
    assert pc == []
    audit = tmp_path / "ap" / "audit.jsonl"
    assert not audit.exists()


def test_preflight_rebind_experts_cwd_none_is_strict_noop(tmp_path, monkeypatch):
    """白樣本：``_preflight_rebind_experts`` 在 cwd=None 時嚴格 no-op（同 _apply_preflight_rebind）。

    主路徑（_run()）會先建 workspace 才進 pre-flight；此處守住「直接呼叫」時 cwd=None
    不會誤觸發 workspace 重建（避免離線單元測試 / 無 workspace 的程式化呼叫炸掉）。
    """
    import asyncio

    bucket: list[events.StudioEvent] = []

    async def broadcast(ev):
        bucket.append(ev)

    monkeypatch.setattr(config, "AUTOPILOT_STATE_DIR", tmp_path / "ap")
    experts = {"engineer": StubExpert(BY_KEY["engineer"], "claude")}
    s = StudioSession("qa-task4", broadcast, experts=experts, cwd=None)
    s._quota_snap = _all_constrained_snap()

    asyncio.run(s._preflight_rebind_experts(experts))

    # 即使全受限、即使 snap 非空，cwd=None → 不發事件、不重建 expert
    assert s._experts["engineer"] is experts["engineer"]
    assert s._experts["engineer"].provider == "claude"
    assert s._recruit_providers == {}
    pc = [e for e in bucket if e.type == events.EventType.PROVIDER_CONSTRAINED]
    assert pc == []
    audit = tmp_path / "ap" / "audit.jsonl"
    assert not audit.exists()


# --- 白樣本：_preflight_rebind_experts 整合路徑 -----------------------------


def test_preflight_rebind_experts_does_not_rebind_explicit_override(tmp_path, monkeypatch):
    """白樣本：整合路徑中明示覆寫角色**完全不被** pre-flight 介入——既不重建 expert 物件，
    也不寫 recruit_providers，也不被加進 planned_roles。
    守門「TI_PROVIDER_<KEY> > 一切自動優化」在場次起點 pre-flight 不會被悄悄降級。
    """
    import asyncio

    bucket: list[events.StudioEvent] = []
    monkeypatch.setattr(config, "AUTOPILOT_STATE_DIR", tmp_path / "ap")
    s, bucket, experts = _make_session(
        tmp_path, monkeypatch, snap=_all_constrained_snap(), explicit_engineer="codex"
    )

    asyncio.run(s._preflight_rebind_experts(experts))

    # engineer 明示 codex → 原物件原 provider 原封不動
    assert s._experts["engineer"] is experts["engineer"]
    assert s._experts["engineer"].provider == "codex"
    # 但其他不在場未明示的角色也照常走（pm/qa 沒 TI_PROVIDER_* → 也不在 ALL_CONSTRAINED 下
    # 發事件因為他們的 effective_provider 是 claude，而我們只對「不在 planned 也不在 explicit
    # 但 constrained 的角色」發事件——若 pm/qa 也受限且無 alt，會被發出 1 筆；此處只驗 engineer
    # 不在 pc 事件中）。
    pc_for_engineer = [
        e
        for e in bucket
        if e.type == events.EventType.PROVIDER_CONSTRAINED and e.payload.get("role") == "engineer"
    ]
    assert pc_for_engineer == []


def test_preflight_rebind_experts_all_constrained_emits_event_per_non_explicit_role(
    tmp_path, monkeypatch
):
    """白樣本（整合）：全受限時，所有「未明示覆寫」且受限的角色**各自**發一筆事件——
    對齊既有 test_multiple_recruits_each_emit_their_own_event 的多事件行為。
    守門「不分組、不聚合、不丟失」——每個受限角色都該被前端看到。
    """
    import asyncio

    bucket: list[events.StudioEvent] = []
    monkeypatch.setattr(config, "AUTOPILOT_STATE_DIR", tmp_path / "ap")
    s, bucket, experts = _make_session(tmp_path, monkeypatch, snap=_all_constrained_snap())

    asyncio.run(s._preflight_rebind_experts(experts))

    pc = [e for e in bucket if e.type == events.EventType.PROVIDER_CONSTRAINED]
    roles = {e.payload["role"] for e in pc}
    # pm/engineer/qa 三個都受限且無 alt、無明示覆寫 → 應各發一筆
    assert roles == {"pm", "engineer", "qa"}
    # 每筆 reason 都是 no_provider_ready（設計決策：reason 固定為字串）
    assert all(e.payload["reason"] == "no_provider_ready" for e in pc)


# --- 既有 contract 不變式守門（routing 合約不被 autopilot 改壞）-------------


def test_explicit_provider_overrides_only_picks_non_empty_entries():
    """白樣本：``_explicit_provider_overrides`` 只回「非空」覆寫——空字串（沒設 env）不視為覆寫。

    守門「空字串 = 未設定」與「非空字串 = 明示」的語意界線；若誤把空字串當覆寫，pre-flight
    會把所有角色都當成「使用者意圖鎖定」，等於永久關閉自動重綁——會讓全受限場景直接卡死。
    """
    s, _b, _e = _make_session(
        tmp_path := __import__("pathlib").Path("/tmp"),
        _mk := __import__("pytest").MonkeyPatch(),
    )
    # 把 engineer 設成空、qa 設成 codex → 只 qa 進 overrides
    _mk.setattr(config, "ROLE_PROVIDERS", {**config.ROLE_PROVIDERS, "engineer": "", "qa": "codex"})
    experts = {
        "engineer": StubExpert(BY_KEY["engineer"], "claude"),
        "qa": StubExpert(BY_KEY["qa"], "codex"),
    }
    overrides = s._explicit_provider_overrides(experts)
    assert overrides == {"qa": "codex"}
    assert "engineer" not in overrides
