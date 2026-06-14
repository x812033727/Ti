"""任務 #3 端到端：OpenAIExpert.speak() retry 重放路徑確認去重層真正接入。

回應審查「致命缺失：去重層未真正接入生產路徑」。本檔不直接呼叫 execute_deduped，而是
透過 speak() 的完整工具迴圈 + run_with_retries 重放，證明：
- retry 重放整輪工具迴圈時，非冪等工具（run_bash append）只實際執行一次（驗收 #2/#3）；
- 每次 speak 重建 DedupCache（per-speak scope 隔離，防跨 speak 結果洩漏，資安追蹤項）。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from studio import config, events, experts, providers
from studio.roles import BY_KEY


def _msg(content=None, tool_calls=None):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=tool_calls))]
    )


def _tc(tool_id, name, arguments):
    return SimpleNamespace(id=tool_id, function=SimpleNamespace(name=name, arguments=arguments))


class ScriptedChat:
    def __init__(self, actions):
        self.actions = actions
        self.calls = 0

    async def __call__(self, messages, tools, model):
        idx = min(self.calls, len(self.actions) - 1)
        self.calls += 1
        action = self.actions[idx]
        if isinstance(action, BaseException):
            raise action
        return action


def collect():
    bucket = []

    async def broadcast(ev):
        bucket.append(ev)

    return bucket, broadcast


@pytest.fixture(autouse=True)
def _no_wait(monkeypatch):
    async def fake_sleep(seconds):
        return None

    monkeypatch.setattr(experts, "_sleep", fake_sleep)


@pytest.fixture
def _cfg(monkeypatch):
    monkeypatch.setattr(config, "EXPERT_RATE_LIMIT_RETRIES", 3)
    monkeypatch.setattr(config, "EXPERT_RATE_LIMIT_BACKOFF", 2.0)
    monkeypatch.setattr(config, "EXPERT_RATE_LIMIT_BACKOFF_CAP", 60.0)
    monkeypatch.setattr(config, "EXPERT_RATE_LIMIT_BACKOFF_JITTER", 0.0)


def _expert(chat, tmp_path):
    return providers.OpenAIExpert(BY_KEY["engineer"], "sess", tmp_path, chat=chat, model="m")


def _rate_limit_err():
    return RuntimeError("Error code: 429 - Rate limit reached")


@pytest.mark.asyncio
async def test_speak_retry_replays_runbash_side_effect_once(_cfg, tmp_path):
    """attempt1 執行 run_bash append 後撞 429 → retry 重放整輪 → 第二次命中快取不重跑。

    若去重層未接入（裸 execute），log.txt 會被 append 兩行（副作用重跑）；接入後只一行。
    """
    args_json = json.dumps({"command": "echo hi >> log.txt"})
    chat = ScriptedChat(
        [
            _msg(tool_calls=[_tc("call_1", "run_bash", args_json)]),  # attempt1: append
            _rate_limit_err(),  # attempt1: 工具後撞限流 → 觸發 retry
            _msg(tool_calls=[_tc("call_2", "run_bash", args_json)]),  # attempt2: 重放（新 tc.id）
            _msg(content="完成"),  # attempt2: 收斂
        ]
    )
    bucket, broadcast = collect()

    out = await _expert(chat, tmp_path).speak("做事", broadcast)

    assert out == "完成"
    assert chat.calls == 4
    # 核心斷言：副作用只發生一次（去重層命中，未重跑 append）
    assert (tmp_path / "log.txt").read_text().splitlines() == ["hi"]


@pytest.mark.asyncio
async def test_legit_duplicate_runbash_in_one_attempt_both_run(_cfg, tmp_path):
    """同一 attempt 內 LLM 合法地下兩次相同 run_bash append → 兩次都執行（不誤去重）。"""
    args_json = json.dumps({"command": "echo dup >> d.txt"})
    chat = ScriptedChat(
        [
            _msg(
                tool_calls=[
                    _tc("c1", "run_bash", args_json),
                    _tc("c2", "run_bash", args_json),  # 同輪第二次合法重複
                ]
            ),
            _msg(content="done"),
        ]
    )
    _, broadcast = collect()

    await _expert(chat, tmp_path).speak("做事", broadcast)

    assert (tmp_path / "d.txt").read_text().splitlines() == ["dup", "dup"]


@pytest.mark.asyncio
async def test_dedup_cache_rebuilt_each_speak(_cfg, tmp_path):
    """per-speak scope：同一 expert 連兩次 speak，第二次的 append 不被第一次快取吞掉。

    證明 speak 入口重建 DedupCache——否則跨 speak 共用會讓第二次 append 命中前次結果、漏副作用。
    """
    args_json = json.dumps({"command": "echo s >> s.txt"})

    def script():
        return ScriptedChat(
            [
                _msg(tool_calls=[_tc("x", "run_bash", args_json)]),
                _msg(content="ok"),
            ]
        )

    expert = _expert(script(), tmp_path)
    _, broadcast = collect()
    await expert.speak("第一輪", broadcast)
    # 換新腳本（模擬下一輪 LLM 回應），同一 expert 物件
    expert._chat = script()
    await expert.speak("第二輪", broadcast)

    # 兩輪各 append 一次 → 兩行（跨 speak 未誤命中）
    assert (tmp_path / "s.txt").read_text().splitlines() == ["s", "s"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
