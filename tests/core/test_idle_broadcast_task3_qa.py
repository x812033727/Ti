"""QA 驗證（任務 #3）：`speak()` 的 `finally` idle 廣播覆蓋全部四條退出路徑。

驗收標準對應：
- idle 狀態廣播覆蓋四條退出路徑：成功／限流耗盡／非限流 api_error／未知例外。

現有 test_providers_openai_retry_task2_qa.py 已對四路徑各斷言「idle 有出現」，
本檔把保證升級為 **idle 必為最後一個狀態廣播**——這正是把 broadcast 放進
`finally`（providers.py:166–167）所要保證的：無論哪條路徑退出，收尾狀態恆為 idle，
新增分支也不會漏（設計決策：finally 是唯一能保證四路徑不漏廣播的位置）。

全程用 ScriptedChat 注入、零 SDK 依賴、零實際等待。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from studio import config, events, experts, providers
from studio.roles import BY_KEY


def _msg(content=None, tool_calls=None):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=tool_calls))]
    )


class ScriptedChat:
    """逐次回應 actions：元素為 Exception→raise；否則→當 response 回傳。用盡後重複最後一個。"""

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
    bucket: list[events.StudioEvent] = []

    async def broadcast(ev):
        bucket.append(ev)

    return bucket, broadcast


def _statuses(bucket):
    return [e.payload["status"] for e in bucket if e.type == events.EventType.EXPERT_STATUS]


def _rate_limit_err():
    return RuntimeError("Error code: 429 - {'error': {'message': 'Rate limit reached'}}")


def _auth_err():
    return RuntimeError("Error code: 401 - invalid api key")


@pytest.fixture(autouse=True)
def _no_wait(monkeypatch):
    async def fake_sleep(seconds):
        pass

    monkeypatch.setattr(experts, "_sleep", fake_sleep)


@pytest.fixture(autouse=True)
def _cfg(monkeypatch):
    monkeypatch.setattr(config, "EXPERT_RATE_LIMIT_RETRIES", 2)
    monkeypatch.setattr(config, "EXPERT_RATE_LIMIT_BACKOFF", 2.0)
    monkeypatch.setattr(config, "EXPERT_RATE_LIMIT_BACKOFF_CAP", 60.0)
    monkeypatch.setattr(config, "EXPERT_RATE_LIMIT_BACKOFF_JITTER", 0.0)


def _expert(chat, tmp_path):
    return providers.OpenAIExpert(BY_KEY["engineer"], "sess", tmp_path, chat=chat, model="m")


# 四路徑：(名稱, actions腳本, 是否會 re-raise)
_PATHS = [
    ("success", [_msg(content="完成發言")], False),
    ("rate_limit_exhausted", [_rate_limit_err()], False),
    ("api_error", [_auth_err()], False),
    ("unknown_exception", [RuntimeError("connection reset by peer")], True),
]


@pytest.mark.parametrize("name,actions,raises", _PATHS, ids=[p[0] for p in _PATHS])
@pytest.mark.asyncio
async def test_finally_idle_is_last_status_on_all_four_paths(name, actions, raises, tmp_path):
    """四條退出路徑收尾狀態恆為 idle（finally 保證，新增分支不漏）。"""
    chat = ScriptedChat(actions)
    bucket, broadcast = collect()
    expert = _expert(chat, str(tmp_path))

    if raises:
        with pytest.raises(RuntimeError):
            await expert.speak("做事", broadcast)
    else:
        await expert.speak("做事", broadcast)

    statuses = _statuses(bucket)
    assert statuses, f"{name}: 應至少廣播一次狀態"
    assert statuses[-1] == "idle", f"{name}: finally 須以 idle 收尾，實得 {statuses}"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
