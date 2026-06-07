"""測試確定性執行工具 runner（不需 LLM）。"""

from __future__ import annotations

import pytest

from studio import runner

# --- 解析執行指令 -------------------------------------------------------


def test_parse_run_command():
    assert runner.parse_run_command("總結…\n執行指令: python main.py") == "python main.py"
    assert runner.parse_run_command("執行指令：`python bmi.py`") == "python bmi.py"
    assert runner.parse_run_command("沒有宣告") is None


# --- 偵測入口 -----------------------------------------------------------


def test_detect_entrypoint_prefers_main(tmp_path):
    (tmp_path / "main.py").write_text("print('hi')")
    (tmp_path / "util.py").write_text("x = 1")
    assert runner.detect_entrypoint(tmp_path) == "main.py"


def test_detect_entrypoint_single_py(tmp_path):
    (tmp_path / "bmi.py").write_text("print('hi')")
    assert runner.detect_entrypoint(tmp_path) == "bmi.py"


def test_detect_entrypoint_none_when_ambiguous(tmp_path):
    (tmp_path / "a.py").write_text("")
    (tmp_path / "b.py").write_text("")
    assert runner.detect_entrypoint(tmp_path) is None


def test_resolve_demo_command(tmp_path):
    assert runner.resolve_demo_command(tmp_path, "python x.py") == "python x.py"
    (tmp_path / "main.py").write_text("")
    assert runner.resolve_demo_command(tmp_path, None) == "python main.py"


# --- 直譯器可攜性（python / python3）-----------------------------------


def test_executable_command_keeps_existing_interpreter(monkeypatch):
    # python 在 PATH 時，指令原封不動。
    monkeypatch.setattr(runner.shutil, "which", lambda name: f"/usr/bin/{name}")
    assert runner._executable_command("python main.py add 3 4") == "python main.py add 3 4"


def test_executable_command_falls_back_to_sys_executable(monkeypatch):
    # python 不在 PATH 時，開頭 token 換成 sys.executable，其餘參數保留。
    monkeypatch.setattr(runner.shutil, "which", lambda name: None)
    monkeypatch.setattr(runner.sys, "executable", "/opt/py/python3")
    out = runner._executable_command("python main.py add 3 4")
    assert out == "/opt/py/python3 main.py add 3 4"


def test_executable_command_ignores_non_python(monkeypatch):
    monkeypatch.setattr(runner.shutil, "which", lambda name: None)
    assert runner._executable_command("echo hi") == "echo hi"


# --- 執行指令 -----------------------------------------------------------


@pytest.mark.asyncio
async def test_run_command_ok(tmp_path):
    r = await runner.run_command(tmp_path, "echo hello")
    assert r.ok
    assert "hello" in r.output
    assert r.exit_code == 0


@pytest.mark.asyncio
async def test_run_command_failure(tmp_path):
    r = await runner.run_command(tmp_path, "exit 3")
    assert not r.ok
    assert r.exit_code == 3


@pytest.mark.asyncio
async def test_run_command_timeout(tmp_path):
    r = await runner.run_command(tmp_path, "sleep 5", timeout=1)
    assert r.timed_out
    assert not r.ok


# --- run_command_exec（任務 #1：參數式 exec helper）-------------------


@pytest.mark.asyncio
async def test_run_command_exec_ok(tmp_path):
    r = await runner.run_command_exec(tmp_path, ["echo", "hello"], sandbox=False)
    assert r.ok
    assert "hello" in r.output
    assert r.exit_code == 0


@pytest.mark.asyncio
async def test_run_command_exec_failure(tmp_path):
    # 不經 shell，用 python 直接設定 exit code
    r = await runner.run_command_exec(
        tmp_path, ["python3", "-c", "import sys; sys.exit(3)"], sandbox=False
    )
    assert not r.ok
    assert r.exit_code == 3


@pytest.mark.asyncio
async def test_run_command_exec_timeout(tmp_path):
    r = await runner.run_command_exec(
        tmp_path, ["python3", "-c", "import time; time.sleep(5)"], timeout=1, sandbox=False
    )
    assert r.timed_out
    assert not r.ok
    assert r.exit_code == -1


@pytest.mark.asyncio
async def test_run_command_exec_runs_in_cwd(tmp_path):
    (tmp_path / "marker.txt").write_text("present")
    r = await runner.run_command_exec(tmp_path, ["ls"], sandbox=False)
    assert r.ok
    assert "marker.txt" in r.output


@pytest.mark.asyncio
async def test_run_command_exec_no_shell_injection(tmp_path):
    """核心安全驗收：argv 內的 shell metacharacters 不被解譯/執行。"""
    payload = "; touch semi `touch backtick` $(touch dollar)\nrm -rf x && touch andand"
    # 把 payload 當單一參數寫進檔案，驗證它原樣保留、且未觸發任何子指令
    r = await runner.run_command_exec(
        tmp_path,
        ["python3", "-c", "import sys,pathlib;pathlib.Path('out.txt').write_text(sys.argv[1])", payload],
        sandbox=False,
    )
    assert r.ok, r.output
    # 沒有任何被注入的指令真的執行
    for ghost in ("semi", "backtick", "dollar", "andand"):
        assert not (tmp_path / ghost).exists(), f"注入指令被執行：{ghost}"
    # payload 原樣（含換行、特殊字元）傳給程式
    assert (tmp_path / "out.txt").read_text() == payload


@pytest.mark.asyncio
async def test_run_command_exec_truncates_output(tmp_path, monkeypatch):
    """複用既有輸出上限邏輯（_truncate / DEMO_MAX_OUTPUT）。"""
    monkeypatch.setattr(runner.config, "DEMO_MAX_OUTPUT", 50)
    r = await runner.run_command_exec(
        tmp_path, ["python3", "-c", "print('x' * 500)"], sandbox=False
    )
    assert r.ok
    assert "已截斷" in r.output
    assert len(r.output) < 200


@pytest.mark.asyncio
async def test_run_command_exec_label(tmp_path):
    # 預設標籤取 argv[0]
    r = await runner.run_command_exec(tmp_path, ["echo", "hi"], sandbox=False)
    assert r.command == "echo"
    # 可覆寫為簡短顯示標籤
    r2 = await runner.run_command_exec(
        tmp_path, ["echo", "hi"], sandbox=False, label="git commit"
    )
    assert r2.command == "git commit"


@pytest.mark.asyncio
async def test_run_command_exec_fail_closed(tmp_path, monkeypatch):
    """沙箱啟用但 bwrap 不存在 → fail-closed，與 shell 路徑語意一致。"""
    monkeypatch.setattr(runner.config, "SANDBOX_BWRAP", "/nonexistent/bwrap")
    r = await runner.run_command_exec(tmp_path, ["touch", "shouldnt"], sandbox=True)
    assert r.timed_out
    assert not r.ok
    assert r.exit_code == -1
    assert "拒絕執行" in r.output
    # 確實沒有真的跑
    assert not (tmp_path / "shouldnt").exists()


@pytest.mark.asyncio
async def test_run_command_exec_sandbox_writes_cwd(tmp_path):
    """沙箱路徑：經 bwrap 執行，cwd 綁定可寫（需環境有 bwrap）。"""
    if not runner.config._sandbox_available():
        pytest.skip("環境無 bwrap，略過沙箱實跑")
    r = await runner.run_command_exec(
        tmp_path, ["python3", "-c", "open('made.txt','w').write('ok')"], sandbox=True
    )
    assert r.ok, r.output
    assert (tmp_path / "made.txt").read_text() == "ok"


@pytest.mark.asyncio
async def test_run_command_exec_empty_argv_raises(tmp_path):
    with pytest.raises(ValueError):
        await runner.run_command_exec(tmp_path, [], sandbox=False)


# --- git ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_git_init_and_commit(tmp_path):
    assert await runner.git_init(tmp_path) is True
    assert (tmp_path / ".git").exists()
    (tmp_path / "f.txt").write_text("hello")
    h = await runner.git_commit(tmp_path, "first commit")
    assert h and len(h) >= 4
    # 無新變更時再 commit 應回 None
    assert await runner.git_commit(tmp_path, "empty") is None


# --- git_commit shell 注入回歸（任務 #2）-------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "evil",
    [
        "fix `touch pwned`",
        "fix $(touch pwned)",
        "fix ; touch pwned",
        "fix && touch pwned",
        "title\nbody; touch pwned",
    ],
)
async def test_git_commit_no_shell_injection(tmp_path, evil):
    """惡意 commit 訊息：指令絕不被執行，但 commit 成功且訊息原樣保留。"""
    assert await runner.git_init(tmp_path) is True
    (tmp_path / "f.txt").write_text("hello")
    h = await runner.git_commit(tmp_path, evil)
    # commit 仍成功
    assert h and len(h) >= 4
    # 注入指令未被執行
    assert not (tmp_path / "pwned").exists(), f"注入被執行：{evil!r}"
    # 訊息原樣寫入 commit message（%B 取完整 body，trailing newline 由 git 正規化）
    body = await runner.run_command_exec(
        tmp_path, ["git", "log", "-1", "--format=%B"], sandbox=False
    )
    assert body.ok
    assert body.output.rstrip("\n") == evil.rstrip("\n")


@pytest.mark.asyncio
async def test_git_commit_multiline_message_preserved(tmp_path):
    """多行 + 特殊字元訊息完整原樣寫入。"""
    assert await runner.git_init(tmp_path) is True
    (tmp_path / "f.txt").write_text("hi")
    msg = "標題行\n\n內文：含 \"引號\" 與 'single' 還有 $VAR 與 #井號\n第三行"
    h = await runner.git_commit(tmp_path, msg)
    assert h
    body = await runner.run_command_exec(
        tmp_path, ["git", "log", "-1", "--format=%B"], sandbox=False
    )
    assert body.output.rstrip("\n") == msg.rstrip("\n")


@pytest.mark.asyncio
async def test_git_commit_no_message_replace_escaping():
    """驗收標準 1：原始碼不應殘留 message.replace 跳脫邏輯。"""
    import inspect

    src = inspect.getsource(runner.git_commit)
    assert "message.replace" not in src
    assert "create_subprocess_shell" not in src


@pytest.mark.asyncio
async def test_git_commit_identity_fallback_when_no_config(tmp_path, monkeypatch):
    """clone 流程：.git 已存在但無 user identity，commit 仍靠 -c 兜底成功。"""
    # .git 已存在 → git_init no-op，不會設 identity
    await runner.run_command_exec(tmp_path, ["git", "init", "-q"], sandbox=False)
    # useConfigOnly=true 關閉 git 的自動 identity 猜測：無明確 identity 必失敗，
    # 因此本測試能真正證明 git_commit 的 -c 兜底有效（而非靠環境自動補）。
    gc = tmp_path / "gitconfig"
    gc.write_text("[user]\n\tuseConfigOnly = true\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gc))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(gc))
    monkeypatch.setenv("HOME", str(tmp_path / "nohome"))
    (tmp_path / "f.txt").write_text("data")
    h = await runner.git_commit(tmp_path, "identity 兜底測試")
    assert h and len(h) >= 4


@pytest.mark.asyncio
async def test_git_commit_fail_closed_when_bwrap_missing(tmp_path, monkeypatch):
    """驗收標準 5：SANDBOX 啟用但 bwrap 缺失 → 整體 fail-closed 回 None，不裸跑。"""
    await runner.run_command_exec(tmp_path, ["git", "init", "-q"], sandbox=False)
    (tmp_path / "f.txt").write_text("data")
    monkeypatch.setattr(runner.config, "SANDBOX_ENABLED", True)
    monkeypatch.setattr(runner.config, "SANDBOX_BWRAP", "/nonexistent/bwrap")
    assert await runner.git_commit(tmp_path, "should fail-closed") is None
    # 沒有任何 commit 真的產生
    log = await runner.run_command_exec(
        tmp_path, ["git", "rev-parse", "--verify", "-q", "HEAD"], sandbox=False
    )
    assert not log.ok


@pytest.mark.asyncio
async def test_git_commit_three_steps_go_through_sandbox(tmp_path, monkeypatch):
    """任務 #3：SANDBOX_ENABLED 時三步 git 操作都經 bwrap 沙箱，且沙箱內 identity 可用。"""
    if not runner.config._sandbox_available():
        pytest.skip("環境無 bwrap，略過沙箱實跑")
    # 先 init 好（.git 已存在）→ git_init no-op，只剩 git_commit 三步會打沙箱
    await runner.run_command_exec(tmp_path, ["git", "init", "-q"], sandbox=False)
    (tmp_path / "f.txt").write_text("data")
    monkeypatch.setattr(runner.config, "SANDBOX_ENABLED", True)

    calls: list[str] = []
    orig = runner._bwrap_prefix

    def spy(cwd):
        calls.append(str(cwd))
        return orig(cwd)

    monkeypatch.setattr(runner, "_bwrap_prefix", spy)
    h = await runner.git_commit(tmp_path, "sandbox 三步")
    # 沙箱內 identity 兜底生效 → commit 成功
    assert h and len(h) >= 4
    # add / commit / rev-parse 三步都走沙箱分支
    assert len(calls) == 3, f"預期三步進沙箱，實際 {len(calls)} 次"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "evil",
    [
        "fix `touch pwned`",
        "fix $(touch pwned)",
        "fix ; touch pwned",
        "head\nbody `touch pwned`",
    ],
)
async def test_git_commit_no_injection_in_real_sandbox(tmp_path, monkeypatch, evil):
    """端到端：SANDBOX_ENABLED 時，惡意訊息經 bwrap 沙箱仍不被執行且 commit 成功。"""
    if not runner.config._sandbox_available():
        pytest.skip("環境無 bwrap，略過沙箱實跑")
    await runner.run_command_exec(tmp_path, ["git", "init", "-q"], sandbox=False)
    (tmp_path / "f.txt").write_text("data")
    monkeypatch.setattr(runner.config, "SANDBOX_ENABLED", True)
    h = await runner.git_commit(tmp_path, evil)
    assert h and len(h) >= 4
    assert not (tmp_path / "pwned").exists(), f"沙箱內注入被執行：{evil!r}"
    body = await runner.run_command_exec(
        tmp_path, ["git", "log", "-1", "--format=%B"], sandbox=False
    )
    assert body.output.rstrip("\n") == evil.rstrip("\n")


@pytest.mark.asyncio
async def test_git_commit_respects_sandbox_disabled(tmp_path, monkeypatch):
    """SANDBOX_ENABLED=False 時三步不進沙箱（沿用 config，非寫死）。"""
    await runner.run_command_exec(tmp_path, ["git", "init", "-q"], sandbox=False)
    (tmp_path / "f.txt").write_text("data")
    monkeypatch.setattr(runner.config, "SANDBOX_ENABLED", False)

    calls: list[str] = []
    monkeypatch.setattr(runner, "_bwrap_prefix", lambda cwd: calls.append(str(cwd)) or [])
    h = await runner.git_commit(tmp_path, "no sandbox")
    assert h and len(h) >= 4
    assert calls == [], "SANDBOX_ENABLED=False 時不應呼叫 bwrap"
