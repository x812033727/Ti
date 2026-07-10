"""測試成果發佈 publisher（純邏輯 + 以 mock 取代實際 IO）。"""

from __future__ import annotations

import pytest

from studio import config, publisher, runner

# --- 純邏輯 -------------------------------------------------------------


@pytest.fixture(autouse=True)
def _git_env_supported(monkeypatch):
    monkeypatch.setattr(config, "TI_GIT_CRED_LEGACY", False)
    monkeypatch.setattr(publisher.git_cred, "_GIT_ENV_SUPPORTED", True)


def test_is_configured(monkeypatch):
    monkeypatch.setattr(config, "GITHUB_TOKEN", "")
    monkeypatch.setattr(config, "PUBLISH_REPO", "")
    assert not publisher.is_configured()
    monkeypatch.setattr(config, "GITHUB_TOKEN", "tok")
    monkeypatch.setattr(config, "PUBLISH_REPO", "o/r")
    assert publisher.is_configured()


def test_branch_name():
    assert publisher.branch_name("abc123") == "ti-studio/abc123"
    assert publisher.branch_name("../evil id") == "ti-studio/evilid"


def test_branch_name_strips_shell_metachars():
    """巡檢 #5：branch 會內插進 `git branch -M {branch}` shell，
    上游 branch_name 須濾掉所有 shell metacharacter，杜絕注入。"""
    evil = "s1; touch pwned $(touch x) `id` && rm -rf / | cat > /etc/x \n\t'\""
    out = publisher.branch_name(evil)
    # 僅保留白名單字元（前綴 + alnum / - / _）
    assert out.startswith("ti-studio/")
    tail = out[len("ti-studio/") :]
    assert all(c.isalnum() or c in "-_" for c in tail), f"殘留危險字元：{out!r}"
    # 明確不含任一 shell metacharacter
    for ch in ";`$()&|><\n\t '\"/\\":
        assert ch not in tail, f"未濾除：{ch!r} in {out!r}"


def test_branch_name_empty_fallback():
    # 全是非法字元時退回 'session'，不產生空 branch
    assert publisher.branch_name("@#%/. ") == "ti-studio/session"


def test_remote_url_and_redact(monkeypatch):
    token = "secrettoken"
    monkeypatch.setattr(config, "GITHUB_TOKEN", token)
    # remote_url 掛了 owner allowlist 護欄：放行本測試用的 owner
    monkeypatch.setattr(config, "PUBLISH_OWNER_ALLOWLIST", frozenset({"octo"}))
    url = publisher.remote_url("octo/repo")
    # 乾淨裸 URL：逐字不含 x-access-token 與 token 明文
    assert url == "https://github.com/octo/repo.git"
    assert "x-access-token" not in url
    assert token not in url


def test_git_auth_env_carries_base64_header():
    """認證改走 env 帶 base64(extraHeader)：驗 header 格式正確、無尾換行，且 b64decode 可反推。"""
    import base64

    token = "secrettoken"
    env = publisher.git_auth_env(token)
    # 走 GIT_CONFIG_* env 注入；先清空 credential.helper，再掛 per-host extraHeader。
    assert env["GIT_CONFIG_COUNT"] == "2"
    assert env["GIT_CONFIG_KEY_0"] == "credential.helper"
    assert env["GIT_CONFIG_VALUE_0"] == ""
    assert env["GIT_CONFIG_KEY_1"] == "http.https://github.com/.extraheader"
    value = env["GIT_CONFIG_VALUE_1"]
    expected_b64 = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    assert value == f"Authorization: Basic {expected_b64}"
    header_b64 = value.rsplit(" ", 1)[-1]
    assert header_b64 == expected_b64
    # 釘死「-n 尾換行坑」：b64 內不得有換行
    assert "\n" not in header_b64
    # 反驗：解回來必須逐字等於 x-access-token:token
    assert base64.b64decode(header_b64).decode() == f"x-access-token:{token}"
    # token 明文不得出現在整包 env 的任一值
    assert all(token not in v for v in env.values())


def test_redact_masks_token_plain_and_base64():
    """redact 須同時遮蔽 token 明文與其 base64 形式，杜絕「改乾淨 URL 卻從 header 漏 token」。"""
    import base64

    token = "secrettoken"
    header_b64 = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    leak = f"token={token} header=Authorization: Basic {header_b64}"
    red = publisher.redact(leak, token)
    assert token not in red
    assert header_b64 not in red
    assert "***" in red


@pytest.mark.asyncio
async def test_push_uses_auth_env_without_leaking_to_argv(monkeypatch, tmp_path):
    """_push 必須只在 git push 帶 extraHeader env，且 argv/command 不含 token 或 b64。"""
    import base64

    token = "secrettoken"
    env = publisher.git_auth_env(token)
    header_b64 = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    calls = []

    async def spy_exec(cwd, argv, **kwargs):
        calls.append((list(argv), dict(kwargs)))
        return runner.RunOutput(
            command=kwargs.get("label", argv[0]),
            exit_code=0,
            output="ok",
            timed_out=False,
        )

    monkeypatch.setattr(runner, "run_command_exec", spy_exec)

    out = await publisher._push(
        tmp_path,
        "ti-studio/s1",
        "https://github.com/octo/repo.git",
        env=env,
    )

    assert out.ok
    assert len(calls) == 4
    assert calls[2][0] == [
        "git",
        "remote",
        "add",
        "ti_publish",
        "https://github.com/octo/repo.git",
    ]
    assert calls[3][0] == ["git", "push", "-u", "ti_publish", "ti-studio/s1"]
    assert calls[3][1]["env"] == env
    assert all(call[1].get("env") is None for call in calls[:3])

    for argv, kwargs in calls:
        joined_argv = "\0".join(argv)
        assert token not in joined_argv
        assert header_b64 not in joined_argv
        assert "Authorization:" not in joined_argv
        assert token not in kwargs["label"]
        assert header_b64 not in kwargs["label"]


@pytest.mark.asyncio
async def test_push_base_uses_auth_env_without_leaking_to_argv(monkeypatch, tmp_path):
    """首次發佈初始化 base 也必須走 env，不可把 header 塞進 argv。"""
    import base64

    token = "secrettoken"
    env = publisher.git_auth_env(token)
    header_b64 = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    calls = []

    async def spy_exec(cwd, argv, **kwargs):
        calls.append((list(argv), dict(kwargs)))
        return runner.RunOutput(
            command=kwargs.get("label", argv[0]),
            exit_code=0,
            output="ok",
            timed_out=False,
        )

    monkeypatch.setattr(runner, "run_command_exec", spy_exec)

    out = await publisher._push_base(
        tmp_path,
        "main",
        "https://github.com/octo/repo.git",
        env=env,
    )

    assert out.ok
    assert calls == [
        (
            ["git", "push", "https://github.com/octo/repo.git", "HEAD:refs/heads/main"],
            {
                "timeout": 120,
                "sandbox": False,
                "label": "git push (init base)",
                "env": env,
            },
        )
    ]
    joined_argv = "\0".join(calls[0][0])
    assert token not in joined_argv
    assert header_b64 not in joined_argv
    assert "Authorization:" not in joined_argv


@pytest.mark.asyncio
async def test_run_command_exec_env_reaches_child_without_entering_command(tmp_path):
    """實跑子行程確認 env 到達；預期值只用 hash 比對，避免 token/b64 進 argv。"""
    import hashlib
    import sys

    token = "secrettoken"
    env = publisher.git_auth_env(token)
    header_b64 = env["GIT_CONFIG_VALUE_1"].rsplit(" ", 1)[-1]
    probe_env = {
        **env,
        "EXPECTED_HEADER_SHA": hashlib.sha256(env["GIT_CONFIG_VALUE_1"].encode()).hexdigest(),
    }

    probe = (
        "import hashlib, os, sys\n"
        "value = os.environ.get('GIT_CONFIG_VALUE_1', '')\n"
        "ok = (\n"
        "    os.environ.get('GIT_CONFIG_COUNT') == '2'\n"
        "    and os.environ.get('GIT_CONFIG_KEY_0') == "
        "'credential.helper'\n"
        "    and os.environ.get('GIT_CONFIG_VALUE_0') == ''\n"
        "    and os.environ.get('GIT_CONFIG_KEY_1') == "
        "'http.https://github.com/.extraheader'\n"
        "    and hashlib.sha256(value.encode()).hexdigest()\n"
        "    == os.environ.get('EXPECTED_HEADER_SHA')\n"
        ")\n"
        "print('env-ok' if ok else 'env-missing')\n"
        "sys.exit(0 if ok else 1)\n"
    )
    out = await runner.run_command_exec(
        tmp_path,
        [sys.executable, "-c", probe],
        timeout=10,
        sandbox=False,
        label="env probe",
        env=probe_env,
    )

    assert out.ok
    assert out.output.strip() == "env-ok"
    assert out.command == "env probe"
    assert token not in out.command
    assert header_b64 not in out.command
    assert token not in probe
    assert header_b64 not in probe


def test_pr_payload():
    p = publisher.pr_payload("做一個 BMI CLI", "ti-studio/x", "main")
    assert p["head"] == "ti-studio/x"
    assert p["base"] == "main"
    assert "BMI" in p["title"]


def test_parse_pr_number():
    assert publisher.parse_pr_number("https://github.com/o/r/pull/42") == 42
    assert publisher.parse_pr_number("https://github.com/o/r/pull/9?foo=1") == 9
    assert publisher.parse_pr_number("") is None
    assert publisher.parse_pr_number(None) is None
    assert publisher.parse_pr_number("https://example.com/no-number") is None


def test_merge_payload():
    p = publisher.merge_payload("ti-studio/x")
    assert p["merge_method"] == "merge"
    assert "ti-studio/x" in p["commit_title"]


# --- publish 流程（mock IO）-------------------------------------------


@pytest.fixture
def _configured(monkeypatch):
    monkeypatch.setattr(config, "GITHUB_TOKEN", "tok")
    monkeypatch.setattr(config, "PUBLISH_REPO", "o/r")
    monkeypatch.setattr(config, "PUBLISH_BASE", "main")
    # owner allowlist 護欄：放行本檔測試用的 owner
    monkeypatch.setattr(config, "PUBLISH_OWNER_ALLOWLIST", frozenset({"o"}))

    # 跳過實際 git init/commit
    async def _noop(*a, **k):
        return True

    monkeypatch.setattr(runner, "git_init", _noop)
    monkeypatch.setattr(runner, "git_commit", _noop)


@pytest.mark.asyncio
async def test_publish_not_configured(monkeypatch):
    monkeypatch.setattr(config, "GITHUB_TOKEN", "")
    monkeypatch.setattr(config, "PUBLISH_REPO", "")
    res = await publisher.publish("/tmp", "s1", "需求")
    assert not res.ok
    assert "未設定" in res.detail


@pytest.mark.asyncio
async def test_publish_push_then_pr(monkeypatch, _configured):
    import base64

    seen = {}

    async def fake_push(cwd, branch, url, **kwargs):
        seen["url"] = url
        seen["env"] = kwargs.get("env")
        assert url == "https://github.com/o/r.git"
        assert "x-access-token" not in url
        assert "tok" not in url
        env = kwargs["env"]
        assert env["GIT_CONFIG_COUNT"] == "2"
        assert env["GIT_CONFIG_KEY_0"] == "credential.helper"
        assert env["GIT_CONFIG_VALUE_0"] == ""
        assert env["GIT_CONFIG_KEY_1"] == "http.https://github.com/.extraheader"
        value = env["GIT_CONFIG_VALUE_1"]
        assert value.startswith("Authorization: Basic ")
        header_b64 = value.rsplit(" ", 1)[-1]
        assert "\n" not in header_b64
        assert base64.b64decode(header_b64).decode() == "x-access-token:tok"
        assert "tok" not in value
        return runner.RunOutput(command="git push", exit_code=0, output="ok", timed_out=False)

    async def fake_pr(payload):
        return True, "https://github.com/o/r/pull/9"

    monkeypatch.setattr(publisher, "_push", fake_push)
    monkeypatch.setattr(publisher, "_open_pr", fake_pr)

    res = await publisher.publish("/tmp", "s1", "需求")
    assert res.ok and res.pushed
    assert res.branch == "ti-studio/s1"
    assert res.pr_url.endswith("/pull/9")
    assert seen["url"] == "https://github.com/o/r.git"
    assert seen["env"]["GIT_CONFIG_VALUE_1"].startswith("Authorization: Basic ")


@pytest.mark.asyncio
async def test_publish_push_fail(monkeypatch, _configured):
    async def fake_push(cwd, branch, url, **kwargs):
        return runner.RunOutput(
            command="git push",
            exit_code=1,
            output="remote: token tok denied",
            timed_out=False,
        )

    monkeypatch.setattr(publisher, "_push", fake_push)
    res = await publisher.publish("/tmp", "s1", "需求")
    assert not res.ok
    assert "push 失敗" in res.detail
    assert "tok" not in res.detail  # token 已遮蔽


@pytest.mark.asyncio
async def test_publish_pr_fail_still_ok(monkeypatch, _configured):
    async def fake_push(cwd, branch, url, **kwargs):
        return runner.RunOutput(command="git push", exit_code=0, output="ok", timed_out=False)

    async def fake_pr(payload):
        return False, "PR 建立失敗（422）：unrelated histories"

    monkeypatch.setattr(publisher, "_push", fake_push)
    monkeypatch.setattr(publisher, "_open_pr", fake_pr)
    res = await publisher.publish("/tmp", "s1", "需求")
    assert res.ok and res.pushed
    assert res.pr_url is None
    assert "PR 建立失敗" in res.detail


# --- merge 流程（mock IO）--------------------------------------------


@pytest.fixture
def _ok_push_pr(monkeypatch):
    async def fake_push(cwd, branch, url, **kwargs):
        return runner.RunOutput(command="git push", exit_code=0, output="ok", timed_out=False)

    async def fake_pr(payload):
        return True, "https://github.com/o/r/pull/7"

    monkeypatch.setattr(publisher, "_push", fake_push)
    monkeypatch.setattr(publisher, "_open_pr", fake_pr)


@pytest.mark.asyncio
async def test_publish_merge_off_does_not_merge(monkeypatch, _configured, _ok_push_pr):
    called = {"n": 0}

    async def fake_flow(number, payload, **kw):
        called["n"] += 1
        return publisher.MergeOutcome.MERGED, "deadbeef"

    monkeypatch.setattr(publisher, "_merge_flow", fake_flow)
    # 預設 merge=False → 不應呼叫 _merge_flow，行為與現在相同
    res = await publisher.publish("/tmp", "s1", "需求")
    assert res.ok and res.pushed and not res.merged
    assert res.pr_url.endswith("/pull/7")
    assert called["n"] == 0
    assert res.outcome is None  # 未嘗試合併


@pytest.mark.asyncio
async def test_publish_merge_success(monkeypatch, _configured, _ok_push_pr):
    async def fake_flow(number, payload, **kw):
        assert number == 7
        assert kw["await_registration"] is True
        assert kw["registration_grace"] == config.PUBLISH_CI_GRACE
        return publisher.MergeOutcome.MERGED, "deadbeef"

    monkeypatch.setattr(publisher, "_merge_flow", fake_flow)
    res = await publisher.publish("/tmp", "s1", "需求", merge=True)
    assert res.ok and res.merged
    assert res.pr_number == 7
    assert "合併" in res.detail
    assert res.outcome == publisher.MergeOutcome.MERGED
    assert res.to_dict()["outcome"] == "merged"


@pytest.mark.asyncio
async def test_publish_merge_conflict_no_raise(monkeypatch, _configured, _ok_push_pr):
    async def fake_flow(number, payload, **kw):
        return publisher.MergeOutcome.CONFLICT, "分支落後或 base 已變動（409）：not mergeable"

    monkeypatch.setattr(publisher, "_merge_flow", fake_flow)
    res = await publisher.publish("/tmp", "s1", "需求", merge=True)
    # 衝突不丟例外：仍 ok（已 push/開 PR），但 merged=False 且帶可讀結局
    assert res.ok and res.pushed and not res.merged
    assert res.pr_url.endswith("/pull/7")
    assert res.outcome == publisher.MergeOutcome.CONFLICT
    assert "未合併" in res.detail
    assert "tok" not in res.detail  # token 已遮蔽


# --- pr_failure_detail：422 無共同歷史是預期情境，不是要丟給使用者的原始 JSON ---


def test_pr_failure_detail_unrelated_history_is_friendly():
    body = (
        '{"message":"Validation Failed","errors":[{"resource":"PullRequest",'
        '"code":"custom","message":"The ti-studio/x branch has no history in common with main"}]}'
    )
    out = publisher.pr_failure_detail(422, body)
    assert "未開 PR" in out and "無共同歷史" in out and "分支已推送保存" in out
    assert "Validation Failed" not in out  # 不把原始 JSON 丟給使用者


def test_pr_failure_detail_other_errors_keep_status_and_body():
    out = publisher.pr_failure_detail(403, "rate limited")
    assert out.startswith("PR 建立失敗（403）")
    assert "rate limited" in out
    # 其他 422（非無共同歷史）仍走一般格式
    out2 = publisher.pr_failure_detail(422, '{"message":"A pull request already exists"}')
    assert out2.startswith("PR 建立失敗（422）")
