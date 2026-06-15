"""QA 驗證（任務 #1）：OpenAI 相容 client 以 max_retries=0 建構。

驗收標準對應：
- studio/providers.py 的 AsyncOpenAI(...) 含 max_retries=0；讓 SDK 內建退避完全讓位給
  run_with_retries，避免雙層疊乘。
- minimax 等 OpenAI 相容 provider 共用同一路徑（_openai_chat → _openai_client_args），
  max_retries=0 一次修到位。
- 反向黑樣本：證明本測試的 fake 有鑑別力——若 providers 漏傳 max_retries，fake 會落回
  「模擬 SDK 預設 2」而被斷言抓出，非恆等於 0 的假綠。

接縫選擇（架構決策）：_openai_chat 用 lazy `import openai`，故 patch 目標必須是
**sys.modules["openai"]**（注入假模組），patch `studio.providers.openai.AsyncOpenAI`
會因 lazy import 未在載入時綁定本地屬性而完全失效（測試恆綠、鑑別力為零）。
環境未安裝 openai 套件，注入假模組同時滿足「零 SDK 依賴」。
"""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from studio import config, providers

# fake AsyncOpenAI 的預設 max_retries＝模擬真實 OpenAI SDK 外部預設值（2）。
# 若 providers.py 漏傳 max_retries=0，建構出的 client.max_retries 就會是這個值 → 被抓回歸。
_SDK_DEFAULT_MAX_RETRIES = 2


def _make_fake_openai_module():
    """產生一個假的 openai module，AsyncOpenAI 記錄建構 kwargs 並暴露 max_retries。"""
    captured: dict = {}

    async def _create(**kwargs):
        # 回傳一個最小合法 response（providers._openai_chat 會 await 它）
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="ok", tool_calls=None))]
        )

    class FakeAsyncOpenAI:
        def __init__(self, *, api_key=None, base_url=None, max_retries=_SDK_DEFAULT_MAX_RETRIES, **kw):
            # max_retries 預設＝SDK 預設 2：providers 不傳就落回 2（反向鑑別力來源）
            self.api_key = api_key
            self.base_url = base_url
            self.max_retries = max_retries
            captured.clear()
            captured.update(
                api_key=api_key, base_url=base_url, max_retries=max_retries, extra=kw
            )
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=_create))

    module = SimpleNamespace(AsyncOpenAI=FakeAsyncOpenAI)
    return module, captured


@pytest.fixture
def fake_openai(monkeypatch):
    module, captured = _make_fake_openai_module()
    monkeypatch.setitem(sys.modules, "openai", module)
    return module, captured


# === 1) 正向：openai 路徑以 max_retries=0 建構 ================================


@pytest.mark.asyncio
async def test_openai_client_built_with_max_retries_zero(fake_openai, monkeypatch):
    """預設 provider（openai）路徑：AsyncOpenAI 收到的 max_retries 必須是 0。"""
    monkeypatch.setattr(config, "PROVIDER", "openai")
    monkeypatch.setattr(config, "OPENAI_API_KEY", "sk-x")
    monkeypatch.setattr(config, "OPENAI_BASE_URL", "")
    _, captured = fake_openai

    await providers._openai_chat([{"role": "user", "content": "hi"}], None, "gpt-x")

    assert captured["max_retries"] == 0  # 退避唯一權威＝run_with_retries
    assert captured["max_retries"] != _SDK_DEFAULT_MAX_RETRIES  # 顯式排除「未設＝預設 2」


# === 2) minimax 共用同一路徑亦生效 ===========================================


@pytest.mark.asyncio
async def test_minimax_client_also_built_with_max_retries_zero(fake_openai, monkeypatch):
    """minimax provider 共用 _openai_chat → 同樣帶 max_retries=0（不個別處理）。"""
    monkeypatch.setattr(config, "MINIMAX_API_KEY", "sk-mm")
    monkeypatch.setattr(config, "MINIMAX_BASE_URL", "https://api.minimax.example/v1")
    _, captured = fake_openai

    await providers._openai_chat(
        [{"role": "user", "content": "hi"}], None, "abab", provider="minimax"
    )

    assert captured["max_retries"] == 0
    assert captured["base_url"] == "https://api.minimax.example/v1"  # 走 minimax 憑證分流
    assert captured["api_key"] == "sk-mm"


# === 3) 反向黑樣本：證明 fake 有鑑別力（非恆等於 0 的假綠）====================


def test_fake_defaults_to_sdk_default_when_max_retries_omitted(fake_openai):
    """直接建構 fake 而不帶 max_retries → 落回模擬 SDK 預設 2。

    這證明：若 providers.py 拿掉 `max_retries=0`，上面的正向斷言會抓到（變成 2），
    本測試組具備真實鑑別力，而非無論如何都通過。
    """
    module, _ = fake_openai
    client = module.AsyncOpenAI(api_key="sk-test")
    assert client.max_retries == _SDK_DEFAULT_MAX_RETRIES  # ＝2，非 0


# === 4) 鑑別力的另一面：lazy import patch 目標正確性自證 ======================


@pytest.mark.asyncio
async def test_patch_target_is_sys_modules_not_local_attr(monkeypatch):
    """自證接縫選擇：patch studio.providers.openai.* 無效（lazy import）；
    必須 patch sys.modules['openai']。本測示範後者才真正攔得到建構。"""
    module, captured = _make_fake_openai_module()
    monkeypatch.setitem(sys.modules, "openai", module)
    monkeypatch.setattr(config, "PROVIDER", "openai")
    monkeypatch.setattr(config, "OPENAI_API_KEY", "sk-x")
    monkeypatch.setattr(config, "OPENAI_BASE_URL", "")

    await providers._openai_chat([{"role": "user", "content": "hi"}], None, "m")

    assert captured, "sys.modules 注入未攔到建構＝接縫錯誤"
    assert captured["max_retries"] == 0


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
