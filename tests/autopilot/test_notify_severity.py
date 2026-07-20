"""例外分級(B5):page=立即推播、digest=僅落檔;watchdog 推播鉤子維持外置契約。

守護不變量:
- 每個程式內 emitter 用到的 kind 都已在 SEVERITY 登記(新事件必須顯式分級)。
- digest 級 kind:send/send_bg 只落檔、零網路——即使 sink 已設定。
- 未登記 kind=page(寧吵勿漏)。
- ti-watchdog.sh 的推播鉤子:僅 TI_WATCHDOG_* env + curl、預設(未設 URL)零行為,
  外置契約(不依賴 python/studio)由既有 test_external_killswitch_contract 守護。
"""

from __future__ import annotations

import re
import urllib.request
from pathlib import Path

import pytest

from studio import config, notify

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _state(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_STATE_DIR", tmp_path / "ap")
    monkeypatch.setattr(config, "NOTIFY_WEBHOOK", "https://hook.example/ti")
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "")
    return tmp_path


def _emitted_kinds() -> set[str]:
    """從 studio/ 原始碼收集 notify.send/send_bg/record 的字面 kind。"""
    kinds: set[str] = set()
    pat = re.compile(r'notify\.(?:send|send_bg|record)\(\s*\n?\s*"([a-z_]+)"')
    for path in (ROOT / "studio").glob("*.py"):
        for m in pat.finditer(path.read_text(encoding="utf-8")):
            kinds.add(m.group(1))
    return kinds


def test_all_emitted_kinds_registered():
    kinds = _emitted_kinds()
    assert kinds, "掃描器抓不到任何 emitter——正則失效?"
    missing = kinds - set(notify.SEVERITY)
    assert not missing, f"事件 kind 未在 SEVERITY 登記(新事件必須顯式分級):{missing}"


def test_digest_kind_never_pushes(monkeypatch):
    calls = []
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: calls.append(1))
    assert notify.send("critic_reject", "退回") is False
    notify.send_bg("gate_failure", "閘門失敗")
    assert not calls, "digest 級 kind 不得推播"
    assert [e["kind"] for e in notify.read_events(1)] == [
        "critic_reject",
        "gate_failure",
    ], "照樣落檔"


def test_unknown_kind_defaults_to_page(monkeypatch):
    sent = []

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: sent.append(1) or _Resp())
    assert notify.send("brand_new_alarm", "x") is True
    assert sent, "未登記 kind 應視為 page 推播(寧吵勿漏)"
    assert notify.severity("brand_new_alarm") == "page"


def test_watchdog_notify_hook_external_contract():
    code = (ROOT / "deploy" / "ti-watchdog.sh").read_text(encoding="utf-8")
    assert "TI_WATCHDOG_NOTIFY_URL" in code, "watchdog 應有可選推播鉤子"
    assert "watchdog_paused" in code, "推播文字應帶 kind 便於對齊 SEVERITY 口徑"
    hook = code.split("例外推播", 1)[1]
    env_reads = set(re.findall(r"\$\{(\w+)[:-]", hook))
    assert env_reads <= {"TI_WATCHDOG_NOTIFY_URL"}, (
        f"鉤子只能讀 TI_WATCHDOG_* 環境變數(外置契約),實際:{env_reads}"
    )


def test_watchdog_script_no_notify_when_url_unset(tmp_path):
    """未設 TI_WATCHDOG_NOTIFY_URL 時腳本行為與過去完全相同(黑白樣本由既有契約測試跑)。"""
    code = (ROOT / "deploy" / "ti-watchdog.sh").read_text(encoding="utf-8")
    assert 'if [ -n "${TI_WATCHDOG_NOTIFY_URL:-}" ]' in code, "推播必須是 opt-in"
