"""任務 #5：去重機制的完整驗收測試（綁定驗收 #2/#3/#6）。

本檔補齊前序任務測試未覆蓋的缺口，皆打在**最終 API**（``tools.execute_deduped`` +
``tools.DedupCache``）與 **providers.speak 生產路徑**上，而非已被取代的舊 helper：

A. 重放去重，副作用只跑一次（驗收 #2）
   - 釘死一條 #3/#4 都沒測的對齊性質：**冪等工具夾在非冪等呼叫之間，不破壞 occurrence
     對齊**（``execute_deduped`` docstring 聲稱「冪等工具夾在中間無害重放、不影響對齊」，
     但無測試鎖定，重構易回歸）。

B. 冪等／讀取型工具不受影響（驗收 #3）
   - ``web_fetch`` 走直通、不經快取（#3 測了 read_file 卻沒測 web_fetch）。

C. 反向黑樣本鎖定已知限制（驗收 #6）
   - 已知限制：**LLM 重放時改變 args → key 變 → 去重漏命中、副作用多跑（at-least-once
     方向）**。#2 QA 雖有同名黑樣本，卻是打在已被取代的舊 ``_deduped_execute`` helper 上；
     本檔在「最終 API」與「providers.speak 整合層」各釘一條，讓限制在真正的生產路徑顯式可見。
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from studio import config, experts, providers, tools
from studio.roles import BY_KEY


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _no_sandbox(monkeypatch):
    """關閉 bwrap sandbox，讓 run_bash 在本機直跑（副作用實際落地）。

    SANDBOX_ENABLED 預設 True；bwrap 缺席的環境下 ``_sandbox_blocked`` 會回傳一段**不以
    ``_ERROR_PREFIXES`` 開頭**的訊息 → 被 ``execute_deduped`` 誤判為成功而入快取、且 append
    從未真正發生，使對齊測試以 ``FileNotFoundError`` 非預期炸掉並掩蓋真正邏輯。測試只關心
    去重對齊，不關心 sandbox，故統一關閉；檔案操作仍隔離在 pytest ``tmp_path`` 內。
    """
    monkeypatch.setattr(config, "SANDBOX_ENABLED", False)


# =============================================================================
# A. 重放去重：副作用只跑一次（驗收 #2）—— 對齊性質鎖定
# =============================================================================


def test_idempotent_calls_between_nonidempotent_dont_break_alignment(tmp_path):
    """冪等工具夾在非冪等呼叫中間，不影響 occurrence 對齊；跨 attempt 重放仍命中。

    場景：每個 attempt 內的呼叫序列為
        run_bash(append A) → read_file(冪等) → run_bash(append B)
    occurrence 序號只在非冪等呼叫間遞增，read_file 夾在中間不應佔用序號。
    重放整輪後，兩個 run_bash 各自對齊回首次 key → append 各只發生一次。
    """
    (tmp_path / "r.txt").write_text("x", encoding="utf-8")
    cache = tools.DedupCache()
    a = {"command": "echo A >> log.txt"}
    b = {"command": "echo B >> log.txt"}
    rd = {"path": "r.txt"}

    async def one_attempt():
        cache.new_attempt()
        await tools.execute_deduped("run_bash", a, tmp_path, cache)
        await tools.execute_deduped("read_file", rd, tmp_path, cache)  # 夾在中間
        await tools.execute_deduped("run_bash", b, tmp_path, cache)

    _run(one_attempt())  # attempt1
    _run(one_attempt())  # attempt2：重放整輪

    # 兩個 run_bash 各自命中、只跑一次 → 各一行
    lines = (tmp_path / "log.txt").read_text().splitlines()
    assert lines == ["A", "B"], f"對齊被破壞，副作用重跑：{lines}"


def test_replay_aligns_per_position_not_global(tmp_path):
    """同一非冪等工具、相同 args 在一個 attempt 內出現兩次（合法重複），重放時仍逐位置對齊。

    attempt 內：append X(#0) → append X(#1)，兩次都跑（合法重複，不誤去重）。
    重放：兩次各對齊回 #0/#1 → 都命中、都不重跑 → 總共仍只兩行。
    """
    cache = tools.DedupCache()
    cmd = {"command": "echo X >> p.txt"}

    async def one_attempt():
        cache.new_attempt()
        await tools.execute_deduped("run_bash", cmd, tmp_path, cache)
        await tools.execute_deduped("run_bash", cmd, tmp_path, cache)

    _run(one_attempt())  # attempt1：兩行
    _run(one_attempt())  # attempt2：重放，兩次都命中、不重跑

    assert (tmp_path / "p.txt").read_text().splitlines() == ["X", "X"]


# =============================================================================
# B. 冪等／讀取型工具不受影響（驗收 #3）
# =============================================================================


def test_web_fetch_not_deduped_passthrough(tmp_path, monkeypatch):
    """web_fetch 唯讀、不納管：直通 execute、不經快取（即使 cache 內有同 base key 的毒值）。"""
    calls = []

    async def fake_fetch(url):
        calls.append(url)
        return f"[HTTP 200] {url}\nbody"

    monkeypatch.setattr(tools, "_research_fetch", fake_fetch)

    cache = tools.DedupCache()
    cache.new_attempt()
    args = {"url": "https://example.com/a"}

    r1 = _run(tools.execute_deduped("web_fetch", args, tmp_path, cache))
    r2 = _run(tools.execute_deduped("web_fetch", args, tmp_path, cache))

    # 不必植毒：is_idempotent("web_fetch")==True → execute_deduped 在 key_for 前就直通，
    # 永不查快取。直接以「fetch 被呼叫兩次」證明未去重即足夠（毒值會是死代碼，反混淆意圖）。
    assert r1 == r2 == "[HTTP 200] https://example.com/a\nbody"
    assert calls == [args["url"], args["url"]]  # 每次都真的抓，不去重


def test_is_idempotent_classification_unchanged(tmp_path):
    """分類契約（驗收 #1 連帶）：唯讀/覆寫型冪等、edit/run_bash 非冪等；未知工具 fail-open。"""
    assert tools.is_idempotent("read_file")
    assert tools.is_idempotent("write_file")
    assert tools.is_idempotent("web_fetch")
    assert tools.is_idempotent("totally_unknown_tool")  # fail-open
    assert not tools.is_idempotent("edit_file")
    assert not tools.is_idempotent("run_bash")


# =============================================================================
# C. 反向黑樣本：已知限制顯式化（驗收 #6）
# =============================================================================


def test_BLACK_replay_changes_args_misses_dedup_final_api(tmp_path):
    """已知限制（最終 API 版）：重放時 LLM 改了 args → key 變 → 漏命中 → 副作用跑兩次。

    這是設計邊界（at-least-once 方向，本機制刻意容忍），非 bug。釘死在 ``execute_deduped`` +
    真實 ``DedupCache`` 上——#2 QA 的同名黑樣本打在已被取代的舊 helper，本條補上最終路徑。
    """
    cache = tools.DedupCache()

    cache.new_attempt()
    _run(tools.execute_deduped("run_bash", {"command": "echo x >> log.txt"}, tmp_path, cache))

    cache.new_attempt()  # 重放，但 LLM 把命令多打一個空格（語意同、字串異）
    _run(tools.execute_deduped("run_bash", {"command": "echo x  >> log.txt"}, tmp_path, cache))

    # 限制顯式化：args 改變 → 去重漏命中 → 副作用跑兩次
    assert (tmp_path / "log.txt").read_text().count("x") == 2


# --- providers.speak 整合層的已知限制黑樣本（最貼近生產路徑）---

def _msg(content=None, tool_calls=None):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=tool_calls))]
    )


def _tc(tool_id, name, arguments):
    return SimpleNamespace(id=tool_id, function=SimpleNamespace(name=name, arguments=arguments))


class _ScriptedChat:
    def __init__(self, actions):
        self.actions = actions
        self.calls = 0

    async def __call__(self, messages, tools_, model):
        idx = min(self.calls, len(self.actions) - 1)
        self.calls += 1
        action = self.actions[idx]
        if isinstance(action, BaseException):
            raise action
        return action


def _collect():
    async def broadcast(ev):
        return None

    return broadcast


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    async def fake_sleep(seconds):
        return None

    monkeypatch.setattr(experts, "_sleep", fake_sleep)


@pytest.fixture
def _retry_cfg(monkeypatch):
    monkeypatch.setattr(config, "EXPERT_RATE_LIMIT_RETRIES", 3)
    monkeypatch.setattr(config, "EXPERT_RATE_LIMIT_BACKOFF", 2.0)
    monkeypatch.setattr(config, "EXPERT_RATE_LIMIT_BACKOFF_CAP", 60.0)
    monkeypatch.setattr(config, "EXPERT_RATE_LIMIT_BACKOFF_JITTER", 0.0)


@pytest.mark.asyncio
async def test_BLACK_speak_replay_changed_args_double_side_effect(_retry_cfg, tmp_path):
    """已知限制（生產路徑版）：speak retry 重放時 LLM 改了 run_bash 命令 → 漏去重 → append 兩行。

    對照 ``test_providers_dedup_task3.py`` 的「同 args 重放只跑一次」：本條證明限制的另一側
    ——args 一旦改變，去重就攔不住，副作用多跑。讓「並非徹底防住重放」在 speak 層顯式可見。
    """
    a1 = json.dumps({"command": "echo hi >> log.txt"})
    a2 = json.dumps({"command": "echo hi  >> log.txt"})  # 重放時改了一個空格
    chat = _ScriptedChat(
        [
            _msg(tool_calls=[_tc("c1", "run_bash", a1)]),  # attempt1：append
            RuntimeError("Error code: 429 - Rate limit reached"),  # 撞限流 → retry
            _msg(tool_calls=[_tc("c2", "run_bash", a2)]),  # attempt2：重放但 args 變了
            _msg(content="完成"),
        ]
    )
    expert = providers.OpenAIExpert(BY_KEY["engineer"], "sess", tmp_path, chat=chat, model="m")

    out = await expert.speak("做事", _collect())

    assert out == "完成"
    assert chat.calls == 4, f"LLM 被呼叫 {chat.calls} 次，預期 4 次（確認 speak 消費完整腳本）"
    # 限制顯式化：args 改變使去重漏命中，append 跑了兩次
    assert (tmp_path / "log.txt").read_text().splitlines() == ["hi", "hi"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
