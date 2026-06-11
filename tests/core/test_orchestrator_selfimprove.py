"""自我改進四機制在主迴圈的整合測試（A 反思 / B 客觀閘門 / D 自我精修 / 預設關＝基線）。

沿用 test_orchestrator 的 StubExpert 範式，直接驅動 _work_task（真實 cwd、ENABLE_GIT=False 使
git_commit/_stalled 變安全 no-op），以 monkeypatch runner.run_command 控制 smoke 的 pass/fail。
全離線、不需 API key、不需 bwrap（sandbox=False）。
"""

from __future__ import annotations

import pytest

from studio import config, events, memory, providers, runner
from studio.orchestrator import LaneContext, StudioSession
from studio.roles import BY_KEY, Role


class StubExpert:
    def __init__(self, role: Role, scripts: list[str]):
        self.role = role
        self._scripts = scripts
        self.calls = 0
        self.prompts: list[str] = []

    async def speak(self, prompt: str, broadcast) -> str:
        self.prompts.append(prompt)
        text = self._scripts[min(self.calls, len(self._scripts) - 1)]
        self.calls += 1
        await broadcast(
            events.expert_message("t", self.role.key, self.role.name, self.role.avatar, text)
        )
        return text

    async def stop(self) -> None:
        pass


def _ro(ok: bool, cmd: str = "run-it", out: str = "EXEC-LOG") -> runner.RunOutput:
    return runner.RunOutput(command=cmd, exit_code=0 if ok else 1, output=out, timed_out=False)


class _Smoke:
    """腳本化 runner.run_command：依序回傳 ok 清單對應的 RunOutput。"""

    def __init__(self, oks: list[bool]):
        self.oks = list(oks)
        self.calls = 0

    async def __call__(self, cwd, command, timeout=None, sandbox=None):
        ok = self.oks[min(self.calls, len(self.oks) - 1)]
        self.calls += 1
        return _ro(ok, cmd=command)


@pytest.fixture(autouse=True)
def _base(monkeypatch):
    monkeypatch.setattr(config, "ENABLE_GIT", False)  # git_commit/_stalled 安全 no-op
    monkeypatch.setattr(config, "DEBATE_ROUNDS", 0)
    monkeypatch.setattr(config, "HUDDLE_ENABLED", False)
    monkeypatch.setattr(config, "CRITIC_ENABLED", False)
    monkeypatch.setattr(config, "OFFLINE_MODE", False)
    monkeypatch.setattr(config, "NOTES_ENABLED", False)
    monkeypatch.setattr(config, "TASK_MAX_ROUNDS", 3)
    monkeypatch.setattr(config, "STALL_ROUNDS", 99)  # 不誤觸停滯收斂
    # 穩健式預設
    monkeypatch.setattr(config, "REFLEXION_ENABLED", False)
    monkeypatch.setattr(config, "OBJECTIVE_GATE", "0")
    monkeypatch.setattr(config, "SELF_REFINE_ITERS", 0)


def _session(tmp_path, monkeypatch, eng, qa, senior):
    monkeypatch.setattr(config, "HISTORY_ROOT", tmp_path / "hist")
    experts = {
        "engineer": StubExpert(BY_KEY["engineer"], eng),
        "qa": StubExpert(BY_KEY["qa"], qa),
        "senior": StubExpert(BY_KEY["senior"], senior),
    }
    bucket: list[events.StudioEvent] = []

    async def bc(ev: events.StudioEvent) -> None:
        bucket.append(ev)

    s = StudioSession("t", bc, experts=experts, cwd=tmp_path)
    ctx = LaneContext(lane_id="main", cwd=tmp_path, experts=experts, branch=None)
    return s, ctx, experts, bucket


def _phases(bucket) -> list[str]:
    return [e.payload.get("phase") for e in bucket if e.type == events.EventType.PHASE_CHANGE]


# --- (B) 客觀閘門：硬性否決 ------------------------------------------------


async def test_gate_vetoes_despite_text_pass(tmp_path, monkeypatch):
    """自測實敗時，即使 QA PASS／高工核可，本輪仍被客觀閘門強制退回。"""
    monkeypatch.setattr(config, "OBJECTIVE_GATE", "1")
    monkeypatch.setattr(runner, "run_command", _Smoke([False]))  # 永遠實敗
    s, ctx, experts, bucket = _session(
        tmp_path,
        monkeypatch,
        eng=["做好了\n執行指令: run-it"],
        qa=["驗證: PASS"],
        senior=["決議: 核可"],
    )
    ok = await s._work_task(ctx, {"id": 1, "title": "做事"}, "計畫")
    assert ok is False  # 文字裁決推翻不了真實 exit code
    assert experts["engineer"].calls > 1  # 進到第 2 輪修正
    assert "【客觀閘門】" in experts["engineer"].prompts[1]
    assert "EXEC-LOG" in experts["engineer"].prompts[1]  # 帶執行紀錄
    assert "客觀閘門" in _phases(bucket)


async def test_gate_off_baseline_passes(tmp_path, monkeypatch):
    """閘門關閉（預設）：自測雖實敗，QA PASS＋核可即第 1 輪通過（既有行為不變）。"""
    monkeypatch.setattr(runner, "run_command", _Smoke([False]))
    s, ctx, experts, bucket = _session(
        tmp_path,
        monkeypatch,
        eng=["做好了\n執行指令: run-it"],
        qa=["驗證: PASS"],
        senior=["決議: 核可"],
    )
    ok = await s._work_task(ctx, {"id": 1, "title": "做事"}, "計畫")
    assert ok is True
    assert experts["engineer"].calls == 1
    assert "客觀閘門" not in _phases(bucket)


async def test_gate_strict_vetoes_when_no_command(tmp_path, monkeypatch):
    """strict：未宣告任何可執行自測指令（smoke=None）也視為未通過。"""
    monkeypatch.setattr(config, "OBJECTIVE_GATE", "strict")
    s, ctx, experts, bucket = _session(
        tmp_path,
        monkeypatch,
        eng=["做好了但沒有執行指令"],  # 無「執行指令:」、cwd 空 → smoke=None
        qa=["驗證: PASS"],
        senior=["決議: 核可"],
    )
    ok = await s._work_task(ctx, {"id": 1, "title": "做事"}, "計畫")
    assert ok is False
    assert "嚴格模式" in experts["engineer"].prompts[1]


# --- (D) 單輪內自我精修 ---------------------------------------------------


async def test_self_refine_fixes_within_round(tmp_path, monkeypatch):
    """自測先敗後成：同一輪內工程師再修一次，smoke 轉綠後交付驗證並通過。"""
    monkeypatch.setattr(config, "SELF_REFINE_ITERS", 1)
    monkeypatch.setattr(runner, "run_command", _Smoke([False, True]))  # 初版敗、精修後成
    s, ctx, experts, bucket = _session(
        tmp_path,
        monkeypatch,
        eng=["初版\n執行指令: run-it", "修好了\n執行指令: run-it"],
        qa=["驗證: PASS"],
        senior=["決議: 核可"],
    )
    ok = await s._work_task(ctx, {"id": 1, "title": "做事"}, "計畫")
    assert ok is True
    assert experts["engineer"].calls == 2  # 初版 + 精修各一次（同一外層輪）
    assert experts["qa"].calls == 1  # 只交付驗證一次（精修不另起一輪）
    assert "【交付前自測未通過" in experts["engineer"].prompts[1]
    assert "EXEC-LOG" in experts["engineer"].prompts[1]
    assert "自我精修" in _phases(bucket)


async def test_self_refine_off_baseline(tmp_path, monkeypatch):
    """精修關閉（預設 0）：自測失敗不就地修，engineer 每輪只一次。"""
    monkeypatch.setattr(runner, "run_command", _Smoke([False]))
    s, ctx, experts, bucket = _session(
        tmp_path,
        monkeypatch,
        eng=["做好了\n執行指令: run-it"],
        qa=["驗證: PASS"],
        senior=["決議: 核可"],
    )
    await s._work_task(ctx, {"id": 1, "title": "做事"}, "計畫")
    assert experts["engineer"].calls == 1
    assert "自我精修" not in _phases(bucket)


# --- (A) 反思記憶注入 -----------------------------------------------------


async def test_reflexion_injects_older_rounds_only(tmp_path, monkeypatch):
    """第 3 輪 prompt 帶第 1 輪蒸餾反思；第 2 輪不帶（exclude_latest，最新原文已在 verbatim）。"""
    monkeypatch.setattr(config, "REFLEXION_ENABLED", True)

    async def fake_complete(system, user, *, session_id, cwd, timeout=120.0):
        return "請改用內建函式並處理空輸入"

    monkeypatch.setattr(providers, "complete_once", fake_complete)
    s, ctx, experts, bucket = _session(
        tmp_path,
        monkeypatch,
        eng=["第1版", "第2版", "第3版"],  # 無執行指令 → smoke=None；閘門關 → 不影響
        qa=["驗證: FAIL", "驗證: FAIL", "驗證: PASS"],
        senior=["決議: 退回", "決議: 退回", "決議: 核可"],
    )
    ok = await s._work_task(ctx, {"id": 5, "title": "做事"}, "計畫")
    assert ok is True  # 第 3 輪通過
    p2, p3 = experts["engineer"].prompts[1], experts["engineer"].prompts[2]
    assert memory.DEFAULT_HEADER in p3 and "[第 1 輪反思]" in p3  # 第 3 輪帶第 1 輪反思
    assert "[第" not in p2  # 第 2 輪不帶（唯一一筆是上一輪，被 exclude_latest 排除）
    assert memory.memory_path(s.session_id).exists()
    assert "反思" in _phases(bucket)


async def test_reflexion_off_no_injection(tmp_path, monkeypatch):
    """反思關閉（預設）：prompt 不含反思區塊，且不產生 memory 檔。"""
    s, ctx, experts, bucket = _session(
        tmp_path,
        monkeypatch,
        eng=["第1版", "第2版", "第3版"],
        qa=["驗證: FAIL", "驗證: PASS"],
        senior=["決議: 退回", "決議: 核可"],
    )
    await s._work_task(ctx, {"id": 5, "title": "做事"}, "計畫")
    assert "過往反思" not in experts["engineer"].prompts[1]
    assert not memory.memory_path(s.session_id).exists()
    assert "反思" not in _phases(bucket)


async def test_reflexion_fallback_when_llm_empty(tmp_path, monkeypatch):
    """反思 LLM 回空：仍存入非空 fallback 反思，下一輪仍帶得到。"""
    monkeypatch.setattr(config, "REFLEXION_ENABLED", True)

    async def empty_complete(system, user, *, session_id, cwd, timeout=120.0):
        return ""

    monkeypatch.setattr(providers, "complete_once", empty_complete)
    s, ctx, experts, _ = _session(
        tmp_path,
        monkeypatch,
        eng=["第1版", "第2版", "第3版"],
        qa=["驗證: FAIL", "驗證: FAIL", "驗證: PASS"],
        senior=["決議: 退回", "決議: 退回", "決議: 核可"],
    )
    await s._work_task(ctx, {"id": 5, "title": "做事"}, "計畫")
    rows = memory.retrieve(s.session_id, 5)
    assert rows and all(r["content"].strip() for r in rows)  # 皆非空（fallback）
    assert "[第 1 輪反思]" in experts["engineer"].prompts[2]


# --- 預設全關＝基線（穩健式） ---------------------------------------------


async def test_all_defaults_match_baseline(tmp_path, monkeypatch):
    """預設下（A/B/D 關、C 開）happy path 行為與既有一致：第 1 輪即過、無新增階段事件。"""
    s, ctx, experts, bucket = _session(
        tmp_path,
        monkeypatch,
        eng=["做好了\n執行指令: run-it"],
        qa=["驗證: PASS"],
        senior=["決議: 核可"],
    )
    # 不 patch run_command：真實執行（cwd 空、無實際程式 → 指令多半失敗，但閘門關不影響裁決）
    ok = await s._work_task(ctx, {"id": 1, "title": "做事"}, "計畫")
    assert ok is True and experts["engineer"].calls == 1
    new_phases = {"客觀閘門", "自我精修", "反思"}
    assert not (new_phases & set(_phases(bucket)))
