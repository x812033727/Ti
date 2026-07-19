"""QA 任務#3：complete_once() 例外分流驗證。

驗收標準（task #3）：
- complete_once() 不再「整段吞掉限流」：限流交由 speak() 內部退避骨幹處理，
  此層僅作兜底（逾時 asyncio.TimeoutError + 未知錯誤 → return ""），維持「永不 raise」。
- complete_once 本層**不**自套第二層 run_with_retries（架構決策：避免雙層重試語義不清）。

本檔驗「行為」而非「程式碼形狀」——以注入假 expert 觀察委派次數與回傳，
並以反向對照（rate-limit 被重試 vs 不被重試）排除假綠。
"""

from __future__ import annotations

import asyncio
import inspect

from studio import config, experts, providers


def _ready(monkeypatch):
    """把 config 調成 openai provider_ready，讓 complete_once 不會在前置防呆早退。"""
    monkeypatch.setattr(config, "OFFLINE_MODE", False)
    monkeypatch.setattr(config, "PROVIDER", "openai")
    monkeypatch.setattr(config, "OPENAI_BASE_URL", "http://localhost:9")
    assert config.provider_ready()


class _FakeExpert:
    """記錄 speak/stop 呼叫次數的假 expert，供觀察 complete_once 的委派行為。"""

    def __init__(self, behavior):
        self._behavior = behavior
        self.speak_calls = 0
        self.stopped = 0

    async def speak(self, user, broadcast):
        self.speak_calls += 1
        return await self._behavior(user, broadcast)

    async def stop(self):
        self.stopped += 1


def _inject(monkeypatch, fake):
    monkeypatch.setattr(providers, "make_expert", lambda role, sid, cwd: fake)


# ---------------------------------------------------------------------------
# 1) 正常路徑：speak 回傳純文字 → 原樣透傳
# ---------------------------------------------------------------------------
async def test_success_passthrough(monkeypatch, tmp_path):
    _ready(monkeypatch)

    async def ok(_u, _b):
        return "反思內容"

    fake = _FakeExpert(ok)
    _inject(monkeypatch, fake)
    out = await providers.complete_once("s", "u", session_id="x", cwd=tmp_path)
    assert out == "反思內容"
    assert fake.speak_calls == 1
    assert fake.stopped == 1  # finally 必清理


# ---------------------------------------------------------------------------
# 2) 逾時路徑：wait_for 觸發 asyncio.TimeoutError → 兜底回 ""（passthrough 語義）
# ---------------------------------------------------------------------------
async def test_timeout_returns_empty(monkeypatch, tmp_path):
    _ready(monkeypatch)

    async def slow(_u, _b):
        await asyncio.sleep(5)
        return "太慢了"

    fake = _FakeExpert(slow)
    _inject(monkeypatch, fake)
    out = await providers.complete_once("s", "u", session_id="x", cwd=tmp_path, timeout=0.05)
    assert out == ""  # 逾時走 fallback，不 raise
    assert fake.stopped == 1


async def test_explicit_timeouterror_returns_empty(monkeypatch, tmp_path):
    """speak 直接拋 asyncio.TimeoutError 也被兜底吞掉（永不 raise）。"""
    _ready(monkeypatch)

    async def raise_to(_u, _b):
        raise asyncio.TimeoutError

    fake = _FakeExpert(raise_to)
    _inject(monkeypatch, fake)
    out = await providers.complete_once("s", "u", session_id="x", cwd=tmp_path)
    assert out == ""


# ---------------------------------------------------------------------------
# 3) 未知例外：兜底回 ""（永不 raise 合約）
# ---------------------------------------------------------------------------
async def test_unknown_exception_backstop(monkeypatch, tmp_path):
    _ready(monkeypatch)

    async def boom(_u, _b):
        raise RuntimeError("unexpected")

    fake = _FakeExpert(boom)
    _inject(monkeypatch, fake)
    out = await providers.complete_once("s", "u", session_id="x", cwd=tmp_path)
    assert out == ""
    assert fake.stopped == 1


# ---------------------------------------------------------------------------
# 4) 核心：限流不在 complete_once 層被「重試吞掉」——本層單次委派，不自套退避
#    反向對照：若此層自套 run_with_retries，speak 會被呼叫多次；應恰好 1 次。
# ---------------------------------------------------------------------------
async def test_no_second_retry_layer_single_delegation(monkeypatch, tmp_path):
    _ready(monkeypatch)

    async def rate_limited(_u, _b):
        # 模擬退避骨幹「耗盡」後上拋的限流訊號（experts 端的 RateLimitSignal）
        raise experts.ExpertRateLimited(None, "429 rate limit", "")

    fake = _FakeExpert(rate_limited)
    _inject(monkeypatch, fake)
    out = await providers.complete_once("s", "u", session_id="x", cwd=tmp_path)
    assert out == ""  # 耗盡才回退空字串
    assert fake.speak_calls == 1  # ★ 關鍵：本層不重試，恰好委派一次（無雙層退避）
    assert fake.stopped == 1


# ---------------------------------------------------------------------------
# 4b) 不靜默吞噬：例外降級回 "" 時必記 warning（含 traceback），供生產診斷
#     （高工指出空字串與真實空回應上層無法區分，log 是唯一補救）
# ---------------------------------------------------------------------------
async def test_degradation_logs_warning(monkeypatch, tmp_path, caplog):
    import logging

    _ready(monkeypatch)

    async def boom(_u, _b):
        raise RuntimeError("api down")

    fake = _FakeExpert(boom)
    _inject(monkeypatch, fake)
    with caplog.at_level(logging.WARNING, logger="studio.providers"):
        out = await providers.complete_once("s", "u", session_id="sess-42", cwd=tmp_path)
    assert out == ""
    warns = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warns, "降級回空字串時必須記 warning，不可靜默吞噬"
    rec = warns[-1]
    assert rec.exc_info is not None, "warning 須帶 exc_info（traceback）供診斷"
    assert "sess-42" in rec.getMessage(), "warning 須含 session 以利定位"


async def test_guard_does_not_log_warning(monkeypatch, tmp_path, caplog):
    """正常前置短路（離線）是預期路徑，不應誤記 warning 製造雜訊。"""
    import logging

    _ready(monkeypatch)
    monkeypatch.setattr(config, "OFFLINE_MODE", True)
    with caplog.at_level(logging.WARNING, logger="studio.providers"):
        out = await providers.complete_once("s", "u", session_id="x", cwd=tmp_path)
    assert out == ""
    assert not [r for r in caplog.records if r.levelno == logging.WARNING]


# ---------------------------------------------------------------------------
# 5) 結構斷言：complete_once 本體不得「實際呼叫」第二套退避入口
#    （架構決策：退避職責只在 speak() 內。用 AST 數 Call 節點，避開 docstring 誤判）
# ---------------------------------------------------------------------------
def _called_names(func):
    """回傳 func 內所有被呼叫的函式名集合（忽略 docstring/註解中的文字）。"""
    import ast
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Name):
                names.add(fn.id)
            elif isinstance(fn, ast.Attribute):
                names.add(fn.attr)
    return names


def test_complete_once_has_no_inline_retry_backbone():
    called = _called_names(providers.complete_once)
    assert "run_with_retries" not in called, "complete_once 不應自套第二層退避骨幹"
    assert "make_retry_config" not in called, "complete_once 不應在本層讀退避 config"


# ---------------------------------------------------------------------------
# 6) 上游分層前提：限流退避骨幹確實存在（否則「交骨幹重試」是空話）。
#    Claude 端 speak() 委派 _speak_with_retries，後者串 run_with_retries + make_retry_config。
# ---------------------------------------------------------------------------
def test_upstream_backbone_exists():
    # speak 委派到 _speak_with_retries（退避不在 speak 本體散落）
    assert "_speak_with_retries" in _called_names(experts.Expert.speak), (
        "speak() 應委派 _speak_with_retries，退避統一收斂於骨幹"
    )
    backbone = _called_names(experts.Expert._speak_with_retries)
    assert "run_with_retries" in backbone, "退避骨幹須呼叫 run_with_retries（限流吸收於此）"
    assert "make_retry_config" in backbone, "退避三參數須源自 make_retry_config 共用旋鈕"
    # make_retry_config 須讀共用 EXPERT_RATE_LIMIT_* 旋鈕，非另起 env
    cfg_src = inspect.getsource(experts.make_retry_config)
    assert "EXPERT_RATE_LIMIT" in cfg_src, "退避 config 須源自共用 EXPERT_RATE_LIMIT_* 旋鈕"


# ---------------------------------------------------------------------------
# 7) 前置防呆維持原樣（無 cwd / 離線）→ ""，且絕不建構 expert（不白等 SDK）
# ---------------------------------------------------------------------------
def _guard_make_expert(monkeypatch):
    """注入會炸的 make_expert：一旦被呼叫即代表防呆沒短路。"""

    def _boom(*a, **k):
        raise AssertionError("防呆未短路，竟建構了 expert")

    monkeypatch.setattr(providers, "make_expert", _boom)


async def test_guard_no_cwd(monkeypatch, tmp_path):
    _ready(monkeypatch)
    _guard_make_expert(monkeypatch)
    assert await providers.complete_once("s", "u", session_id="x", cwd=None) == ""


async def test_guard_offline(monkeypatch, tmp_path):
    _ready(monkeypatch)
    monkeypatch.setattr(config, "OFFLINE_MODE", True)
    _guard_make_expert(monkeypatch)
    assert await providers.complete_once("s", "u", session_id="x", cwd=tmp_path) == ""
