"""providers.complete_once：provider 無關的單輪呼叫；防呆回空 + openai 分支 + 永不 raise。"""

from __future__ import annotations

from studio import config, providers


async def test_returns_empty_when_no_cwd():
    assert await providers.complete_once("s", "u", session_id="x", cwd=None) == ""


async def test_returns_empty_when_offline(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "OFFLINE_MODE", True)
    assert await providers.complete_once("s", "u", session_id="x", cwd=tmp_path) == ""


async def test_openai_branch_single_turn(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "OFFLINE_MODE", False)
    monkeypatch.setattr(config, "PROVIDER", "openai")
    captured: dict = {}

    class _Msg:
        content = "反思結果"
        tool_calls = None

    class _Choice:
        message = _Msg()

    class _Resp:
        choices = [_Choice()]

    async def fake_chat(messages, tools_, model):
        captured["messages"] = messages
        captured["tools"] = tools_
        return _Resp()

    monkeypatch.setattr(providers, "_openai_chat", fake_chat)
    out = await providers.complete_once("你是反思器", "覆盤這輪", session_id="x", cwd=tmp_path)
    assert out == "反思結果"
    assert captured["messages"][0] == {"role": "system", "content": "你是反思器"}
    assert any(m["role"] == "user" and m["content"] == "覆盤這輪" for m in captured["messages"])
    # oneshot（allowed_tools=[]）只有唯讀 read_file 基線，絕無改檔/執行類工具。
    tool_names = {t["function"]["name"] for t in captured["tools"]}
    assert "write_file" not in tool_names and "run_bash" not in tool_names


async def test_never_raises_on_chat_error(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "OFFLINE_MODE", False)
    monkeypatch.setattr(config, "PROVIDER", "openai")

    async def boom(*a, **k):
        raise RuntimeError("api down")

    monkeypatch.setattr(providers, "_openai_chat", boom)
    assert await providers.complete_once("s", "u", session_id="x", cwd=tmp_path) == ""
