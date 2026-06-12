"""roadmap 階段二「研究能力控管」測試（全離線，不連網、不需真 LLM）。

涵蓋：
- roles.effective_tools：TI_RESEARCH_TOOLS 開/關時各角色的有效工具清單。
- tools.specs_for / web_fetch spec 對應。
- tools.research_url_check：scheme／私網 SSRF／網域白名單黑白樣本。
- tools._research_fetch（經 _http_get 注入縫）：截斷、剝 HTML、redirect 至私網被拒、逾時降級。
- experts._auto_allow_tool：WebFetch 經白名單 Deny/Allow，其餘工具一律 Allow（原行為）。
- settings：兩個研究欄已註冊、select 非法值被忽略。
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from studio import config, tools
from studio.roles import BY_KEY, effective_tools

PM = BY_KEY["pm"]
ENGINEER = BY_KEY["engineer"]
QA = BY_KEY["qa"]
SENIOR = BY_KEY["senior"]
RESEARCHER = BY_KEY["researcher"]


# ---------- effective_tools ----------
def test_effective_tools_default_unchanged(monkeypatch):
    monkeypatch.setattr(config, "RESEARCH_TOOLS_ENABLED", False)
    for r in (PM, ENGINEER, QA, SENIOR, RESEARCHER):
        assert effective_tools(r) == list(r.allowed_tools)


def test_effective_tools_enabled_adds_for_eng_senior(monkeypatch):
    monkeypatch.setattr(config, "RESEARCH_TOOLS_ENABLED", True)
    for r in (ENGINEER, SENIOR):
        eff = effective_tools(r)
        assert "WebSearch" in eff and "WebFetch" in eff
        assert eff.count("WebSearch") == 1 and eff.count("WebFetch") == 1
        # 原有工具不被移除
        assert set(r.allowed_tools) <= set(eff)
    # 非工程師/高工角色不受影響
    assert effective_tools(PM) == list(PM.allowed_tools)
    assert effective_tools(QA) == list(QA.allowed_tools)


def test_effective_tools_researcher_no_dup(monkeypatch):
    """researcher 本就含 WebSearch/WebFetch，且不在附加名單內 → 原樣、不重複。"""
    monkeypatch.setattr(config, "RESEARCH_TOOLS_ENABLED", True)
    assert effective_tools(RESEARCHER) == list(RESEARCHER.allowed_tools)


def test_effective_tools_no_mutation(monkeypatch):
    """不得就地改動 frozen 的 role.allowed_tools。"""
    monkeypatch.setattr(config, "RESEARCH_TOOLS_ENABLED", True)
    before = list(ENGINEER.allowed_tools)
    effective_tools(ENGINEER)
    assert list(ENGINEER.allowed_tools) == before


# ---------- specs_for / web_fetch ----------
def test_specs_for_includes_web_fetch_when_webfetch():
    names = {s["function"]["name"] for s in tools.specs_for(["Read", "WebFetch"])}
    assert "web_fetch" in names


def test_specs_for_excludes_web_fetch_without():
    names = {s["function"]["name"] for s in tools.specs_for(["Read", "Bash"])}
    assert "web_fetch" not in names


# ---------- research_url_check（純函式，無網路）----------
@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://host/x",
        "http://127.0.0.1/",
        "http://10.0.0.8/",
        "http://[::1]/",
        "http://169.254.1.1/",  # link-local
        "http://192.168.1.1/",
        "http://0.0.0.0/",  # unspecified
        "https://[::ffff:127.0.0.1]/",  # IPv4-mapped loopback
        "notaurl",  # 無 scheme/host
    ],
)
def test_research_url_check_rejects(monkeypatch, url):
    monkeypatch.setattr(config, "RESEARCH_ALLOWED_DOMAINS", [])
    assert tools.research_url_check(url) is not None


def test_research_url_check_allows_public_when_no_whitelist(monkeypatch):
    monkeypatch.setattr(config, "RESEARCH_ALLOWED_DOMAINS", [])
    assert tools.research_url_check("https://docs.python.org/3/") is None


def test_research_url_check_whitelist(monkeypatch):
    monkeypatch.setattr(config, "RESEARCH_ALLOWED_DOMAINS", ["example.com"])
    assert tools.research_url_check("http://evil.com/") is not None  # 不在白名單
    assert tools.research_url_check("https://example.com/x") is None  # 完全相符
    assert tools.research_url_check("https://docs.example.com/x") is None  # 子網域
    assert tools.research_url_check("https://notexample.com/") is not None  # 非子網域，須拒


# ---------- _research_fetch（經 _http_get 注入縫）----------
class _FakeResp:
    def __init__(self, status_code=200, headers=None, text=""):
        self.status_code = status_code
        self.headers = headers or {}
        self.text = text


async def test_web_fetch_truncates_and_strips_html(monkeypatch):
    monkeypatch.setattr(config, "RESEARCH_ALLOWED_DOMAINS", [])
    monkeypatch.setattr(config, "RESEARCH_FETCH_MAX_CHARS", 30)
    monkeypatch.setattr(tools, "_resolved_addr_reason", lambda url: None)
    html = "<html><script>evil()</script><body>" + ("字" * 100) + "</body></html>"

    async def fake_get(url, timeout):
        return _FakeResp(200, {"content-type": "text/html; charset=utf-8"}, html)

    monkeypatch.setattr(tools, "_http_get", fake_get)
    out = await tools.execute("web_fetch", {"url": "https://example.com/x"}, Path("/tmp"))
    assert "evil()" not in out  # <script> 內容被剝
    assert "<body>" not in out  # 標籤被剝
    assert "（已截斷）" in out


async def test_web_fetch_redirect_to_private_rejected(monkeypatch):
    monkeypatch.setattr(config, "RESEARCH_ALLOWED_DOMAINS", [])
    monkeypatch.setattr(tools, "_resolved_addr_reason", lambda url: None)

    async def fake_get(url, timeout):
        # 先回 302 指向私網位址；逐跳重驗時 research_url_check 應擋下（IP 字面值，免 DNS）。
        return _FakeResp(302, {"location": "http://10.0.0.1/secret"}, "")

    monkeypatch.setattr(tools, "_http_get", fake_get)
    out = await tools.execute("web_fetch", {"url": "https://example.com/x"}, Path("/tmp"))
    assert "錯誤：研究抓取被拒" in out
    assert "非公開網路" in out


async def test_web_fetch_timeout_degrades(monkeypatch):
    monkeypatch.setattr(config, "RESEARCH_ALLOWED_DOMAINS", [])
    monkeypatch.setattr(tools, "_resolved_addr_reason", lambda url: None)

    async def fake_get(url, timeout):
        raise TimeoutError("timed out")

    monkeypatch.setattr(tools, "_http_get", fake_get)
    out = await tools.execute("web_fetch", {"url": "https://example.com/x"}, Path("/tmp"))
    assert out.startswith("錯誤：研究抓取失敗")  # 降級訊息、不 raise


async def test_web_fetch_blocked_url_not_fetched(monkeypatch):
    """URL 一開始就違規 → 連 _http_get 都不該被呼叫。"""
    monkeypatch.setattr(config, "RESEARCH_ALLOWED_DOMAINS", [])
    called = {"n": 0}

    async def fake_get(url, timeout):
        called["n"] += 1
        return _FakeResp()

    monkeypatch.setattr(tools, "_http_get", fake_get)
    out = await tools.execute("web_fetch", {"url": "http://127.0.0.1/"}, Path("/tmp"))
    assert "錯誤：研究抓取被拒" in out
    assert called["n"] == 0


def test_summarize_web_fetch():
    assert tools.summarize("web_fetch", {"url": "https://x.com/page"}).startswith("網路抓取 ")


# ---------- experts._auto_allow_tool（WebFetch 管控）----------
@pytest.fixture
def fake_perm_sdk(monkeypatch):
    mod = types.ModuleType("claude_agent_sdk")

    class PermissionResultAllow:
        pass

    class PermissionResultDeny:
        def __init__(self, message=""):
            self.message = message

    mod.PermissionResultAllow = PermissionResultAllow
    mod.PermissionResultDeny = PermissionResultDeny
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", mod)
    return mod


async def test_auto_allow_webfetch_outside_whitelist_denied(monkeypatch, fake_perm_sdk):
    from studio import experts

    monkeypatch.setattr(config, "RESEARCH_ALLOWED_DOMAINS", ["example.com"])
    res = await experts._auto_allow_tool("WebFetch", {"url": "http://evil.com/"}, None)
    assert isinstance(res, fake_perm_sdk.PermissionResultDeny)


async def test_auto_allow_webfetch_inside_whitelist_allowed(monkeypatch, fake_perm_sdk):
    from studio import experts

    monkeypatch.setattr(config, "RESEARCH_ALLOWED_DOMAINS", ["example.com"])
    res = await experts._auto_allow_tool("WebFetch", {"url": "https://docs.example.com/x"}, None)
    assert isinstance(res, fake_perm_sdk.PermissionResultAllow)


async def test_auto_allow_webfetch_private_denied_even_empty_whitelist(monkeypatch, fake_perm_sdk):
    from studio import experts

    monkeypatch.setattr(config, "RESEARCH_ALLOWED_DOMAINS", [])
    res = await experts._auto_allow_tool("WebFetch", {"url": "http://127.0.0.1/"}, None)
    assert isinstance(res, fake_perm_sdk.PermissionResultDeny)


async def test_auto_allow_other_tools_always_allowed(fake_perm_sdk):
    from studio import experts

    for name, inp in (("Bash", {"command": "ls"}), ("Write", {"file_path": "/w/a"})):
        res = await experts._auto_allow_tool(name, inp, None)
        assert isinstance(res, fake_perm_sdk.PermissionResultAllow)


# ---------- settings 註冊與非法值 ----------
def test_settings_research_fields_registered():
    from studio import settings

    assert "TI_RESEARCH_TOOLS" in settings.ALLOWED
    assert "TI_RESEARCH_ALLOWED_DOMAINS" in settings.ALLOWED


def test_settings_research_tools_rejects_illegal(monkeypatch):
    from studio import settings

    written: dict[str, str] = {}
    monkeypatch.setattr(settings, "write_secret_file", lambda p, k, v: written.__setitem__(k, v))
    monkeypatch.setattr(settings.config, "reload", lambda: None)
    monkeypatch.setattr(settings, "read", lambda: {"fields": []})
    settings.update({"TI_RESEARCH_TOOLS": "9"})  # 非法
    assert written == {}
    settings.update({"TI_RESEARCH_TOOLS": "1"})  # 合法
    assert written == {"TI_RESEARCH_TOOLS": "1"}
