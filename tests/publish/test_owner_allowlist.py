"""發佈與建庫的 owner allowlist 安全護欄。

守護目標：autopilot/publisher 只能 push／merge／deploy allowlist owner（預設 x812033727）
底下的 repo；允許在同 owner 底下建立**全新** repo（SaaS 工作流）；**絕不**推送／污染任何
其他既有 repo。三個 chokepoint：set_repo_override／remote_url／_ensure_repo。
"""

from __future__ import annotations

import pytest

from studio import autopilot, config, publisher, repo_ident, runner

_DEFAULT = frozenset({"x812033727"})


@pytest.fixture
def _default_allowlist(monkeypatch):
    monkeypatch.setattr(config, "PUBLISH_OWNER_ALLOWLIST", _DEFAULT)


# --- repo_ident：owner 解析的單一真相 -------------------------------------


def test_repo_owner_parses_bare_https_and_ssh():
    assert repo_ident.repo_owner("x812033727/Ti") == "x812033727"
    assert repo_ident.repo_owner("https://github.com/X812033727/Ti.git") == "x812033727"
    assert repo_ident.repo_owner("git@github.com:x812033727/Ti.git") == "x812033727"
    assert repo_ident.repo_owner("github.com/x812033727/Ti") == "x812033727"


def test_repo_owner_fails_closed_on_non_github_or_garbage():
    # 同 path 非 GitHub host 不可解析出 owner（防偽造同 path host 繞過 allowlist）
    assert repo_ident.repo_owner("https://evil.example/x812033727/Ti.git") == ""
    assert repo_ident.repo_owner("git@evil.example:x812033727/Ti.git") == ""
    assert repo_ident.repo_owner("") == ""
    assert repo_ident.repo_owner("not-a-repo") == ""
    assert repo_ident.repo_owner("a/b/c") == ""


def test_autopilot_repo_key_is_repo_ident_single_source():
    """autopilot 的 _repo_key 必須就是 repo_ident.repo_key（單一真相，不可分歧）。"""
    assert autopilot._repo_key is repo_ident.repo_key


def test_config_allowlist_default_and_csv_reload(monkeypatch):
    """TI_PUBLISH_OWNER_ALLOWLIST：csv → 小寫 frozenset，預設 x812033727，進 reload()。"""
    monkeypatch.setenv("TI_PUBLISH_OWNER_ALLOWLIST", " Alice ,BOB,, ")
    config.reload()
    try:
        assert config.PUBLISH_OWNER_ALLOWLIST == frozenset({"alice", "bob"})
    finally:
        monkeypatch.delenv("TI_PUBLISH_OWNER_ALLOWLIST", raising=False)
        config.reload()
    assert config.PUBLISH_OWNER_ALLOWLIST == _DEFAULT


# --- assert_repo_allowed --------------------------------------------------


def test_assert_repo_allowed_accepts_allowlisted_owner(_default_allowlist):
    for repo in (
        "x812033727/Ti",
        "X812033727/AnyNewRepo",
        "https://github.com/x812033727/Ti.git",
        "git@github.com:x812033727/Ti.git",
    ):
        publisher.assert_repo_allowed(repo)  # 不應 raise


def test_assert_repo_allowed_blocks_other_owner(_default_allowlist):
    with pytest.raises(ValueError, match="allowlist"):
        publisher.assert_repo_allowed("otherowner/Ti")


def test_assert_repo_allowed_blocks_unparsable_and_foreign_host(_default_allowlist):
    for repo in ("", "not-a-repo", "https://evil.example/x812033727/Ti.git"):
        with pytest.raises(ValueError, match="allowlist"):
            publisher.assert_repo_allowed(repo)


# --- chokepoint 1：set_repo_override --------------------------------------


def test_set_repo_override_allows_allowlisted_owner(_default_allowlist, monkeypatch):
    monkeypatch.setattr(config, "PUBLISH_REPO", "")
    token = publisher.set_repo_override("x812033727/Ti")
    try:
        assert publisher.current_repo() == "x812033727/Ti"
    finally:
        publisher.reset_repo_override(token)


def test_set_repo_override_blocks_other_owner(_default_allowlist, monkeypatch):
    monkeypatch.setattr(config, "PUBLISH_REPO", "")
    with pytest.raises(ValueError, match="allowlist"):
        publisher.set_repo_override("otherowner/Ti")
    assert publisher.current_repo() == ""  # 覆寫未生效


def test_set_repo_override_empty_clears_without_guard(_default_allowlist):
    # None／空字串＝清除覆寫，不觸發護欄（autopilot finally 還原路徑依賴此語意）
    token = publisher.set_repo_override(None)
    publisher.reset_repo_override(token)
    token = publisher.set_repo_override("")
    publisher.reset_repo_override(token)


# --- chokepoint 2：remote_url ----------------------------------------------


def test_remote_url_allows_allowlisted_owner(_default_allowlist):
    url = publisher.remote_url("x812033727/Ti", "tok")
    assert url == "https://x-access-token:tok@github.com/x812033727/Ti.git"


def test_remote_url_blocks_other_owner(_default_allowlist):
    with pytest.raises(ValueError, match="allowlist"):
        publisher.remote_url("otherowner/Ti", "tok")


# --- chokepoint 3：_ensure_repo --------------------------------------------


class _FakeResp:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = ""

    def json(self):
        return self._payload


def _fake_httpx(
    monkeypatch, *, repo_status, branch_status=200, user_login="x812033727", create_status=201
):
    """以 URL 路由的假 httpx.AsyncClient；回傳 (method, url) 呼叫紀錄供斷言。"""
    calls: list[tuple[str, str]] = []

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None, params=None):
            calls.append(("GET", url))
            if url.endswith("/user"):
                return _FakeResp(200, {"login": user_login})
            if "/branches/" in url:
                return _FakeResp(branch_status)
            return _FakeResp(repo_status)

        async def post(self, url, json=None, headers=None):
            calls.append(("POST", url))
            return _FakeResp(create_status, {"full_name": json and json.get("name")})

    monkeypatch.setattr("httpx.AsyncClient", _Client)
    return calls


async def test_ensure_repo_blocks_other_owner_before_any_api_call(_default_allowlist, monkeypatch):
    calls = _fake_httpx(monkeypatch, repo_status=200)
    with pytest.raises(ValueError, match="allowlist"):
        await publisher._ensure_repo("otherowner/Ti", "main")
    assert calls == []  # 連查詢都不該發生


async def test_ensure_repo_known_target_stays_ready(_default_allowlist, monkeypatch):
    """既有 repo 是已知發佈目標（PUBLISH_REPO）→ 照常 ready。"""
    monkeypatch.setattr(config, "PUBLISH_REPO", "x812033727/Ti")
    monkeypatch.setattr(config, "AUTOPILOT_REPO", "x812033727/Ti")
    _fake_httpx(monkeypatch, repo_status=200, branch_status=200)
    assert await publisher._ensure_repo("x812033727/Ti", "main") == "ready"


async def test_ensure_repo_existing_repo_not_known_target_refused(_default_allowlist, monkeypatch):
    """既有 repo 且非 AUTOPILOT_REPO／PUBLISH_REPO → 拒推（即使 owner 相同）。"""
    monkeypatch.setattr(config, "PUBLISH_REPO", "x812033727/Ti")
    monkeypatch.setattr(config, "AUTOPILOT_REPO", "x812033727/Ti")
    calls = _fake_httpx(monkeypatch, repo_status=200)
    out = await publisher._ensure_repo("x812033727/some-existing", "main")
    assert out.startswith("unavailable")
    assert "拒絕推送" in out
    # 拒推後不應再查 base 分支（不進發佈流程）
    assert not any("/branches/" in url for _, url in calls)


async def test_ensure_repo_creates_new_repo_same_owner(_default_allowlist, monkeypatch):
    """同 owner 底下的全新 repo：create 成功（201）即放行（SaaS 工作流），不做二次 GET。"""
    monkeypatch.setattr(config, "PUBLISH_REPO", "x812033727/Ti")
    monkeypatch.setattr(config, "AUTOPILOT_REPO", "x812033727/Ti")
    calls = _fake_httpx(monkeypatch, repo_status=404, create_status=201)
    out = await publisher._ensure_repo("x812033727/brand-new", "main")
    assert out == "empty"
    assert ("POST", "https://api.github.com/user/repos") in calls
    # create 成功後以回傳為憑據放行，不得用二次 GET 判斷（避免競態）
    assert calls[-1][0] == "POST"


async def test_ensure_repo_create_failure_stays_unavailable(_default_allowlist, monkeypatch):
    _fake_httpx(monkeypatch, repo_status=404, create_status=403)
    out = await publisher._ensure_repo("x812033727/brand-new", "main")
    assert out.startswith("unavailable")


async def test_ensure_repo_owner_not_token_user_stays_unavailable(_default_allowlist, monkeypatch):
    _fake_httpx(monkeypatch, repo_status=404, user_login="someone-else")
    out = await publisher._ensure_repo("x812033727/brand-new", "main")
    assert out.startswith("unavailable")
    assert "token" in out


# --- publish() 端到端（護欄不外拋、擋在任何 push 之前）-----------------------


@pytest.fixture
def _git_noop(monkeypatch):
    async def _noop(*a, **k):
        return runner.RunOutput(command="git", exit_code=0, output="ok", timed_out=False)

    monkeypatch.setattr(runner, "git_init", _noop)
    monkeypatch.setattr(runner, "git_commit", _noop)
    monkeypatch.setattr(runner, "git_sanitize_workspace", _noop)
    monkeypatch.setattr(runner, "ruff_format_workspace", _noop)


async def test_publish_to_other_owner_fails_closed_without_push(
    _default_allowlist, _git_noop, monkeypatch
):
    monkeypatch.setattr(config, "GITHUB_TOKEN", "tok")
    monkeypatch.setattr(config, "PUBLISH_REPO", "")
    pushed = {"n": 0}

    async def spy_push(*a, **k):
        pushed["n"] += 1
        return runner.RunOutput(command="git push", exit_code=0, output="ok", timed_out=False)

    monkeypatch.setattr(publisher, "_push", spy_push)
    monkeypatch.setattr(publisher, "_push_base", spy_push)

    res = await publisher.publish("/tmp", "s1", "需求", repo="otherowner/Ti")
    assert not res.ok
    assert "allowlist" in res.detail
    assert pushed["n"] == 0  # 任何 push 都不得發生
    assert publisher.current_repo() == ""  # 覆寫未殘留


async def test_publish_existing_foreign_repo_refused_via_ensure(
    _default_allowlist, _git_noop, monkeypatch
):
    """同 owner 但『既有且非已知目標』的 repo：走 _ensure_repo 拒推，發佈失敗且無 push。"""
    monkeypatch.setattr(config, "GITHUB_TOKEN", "tok")
    monkeypatch.setattr(config, "PUBLISH_REPO", "x812033727/Ti")
    monkeypatch.setattr(config, "AUTOPILOT_REPO", "x812033727/Ti")
    monkeypatch.setattr(config, "PUBLISH_BASE", "main")
    _fake_httpx(monkeypatch, repo_status=200)
    pushed = {"n": 0}

    async def spy_push(*a, **k):
        pushed["n"] += 1
        return runner.RunOutput(command="git push", exit_code=0, output="ok", timed_out=False)

    monkeypatch.setattr(publisher, "_push", spy_push)
    monkeypatch.setattr(publisher, "_push_base", spy_push)

    res = await publisher.publish("/tmp", "s1", "需求", repo="x812033727/some-existing")
    assert not res.ok
    assert "無法發佈" in res.detail
    assert pushed["n"] == 0


async def test_publish_new_repo_same_owner_allowed_end_to_end(
    _default_allowlist, _git_noop, monkeypatch
):
    """同 owner 全新 repo：自動建立（create 201）→ 首次發佈初始化 base，整段放行。"""
    monkeypatch.setattr(config, "GITHUB_TOKEN", "tok")
    monkeypatch.setattr(config, "PUBLISH_REPO", "x812033727/Ti")
    monkeypatch.setattr(config, "AUTOPILOT_REPO", "x812033727/Ti")
    monkeypatch.setattr(config, "PUBLISH_BASE", "main")
    _fake_httpx(monkeypatch, repo_status=404, create_status=201)
    seen = {}

    async def fake_push_base(cwd, base, url):
        seen["url"] = url
        return runner.RunOutput(command="git push", exit_code=0, output="ok", timed_out=False)

    monkeypatch.setattr(publisher, "_push_base", fake_push_base)

    res = await publisher.publish("/tmp", "s1", "需求", repo="x812033727/brand-new")
    assert res.ok and res.pushed and res.merged
    assert "首次發佈" in res.detail
    assert "x812033727/brand-new" in seen["url"]
