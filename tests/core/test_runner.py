"""測試確定性執行工具 runner（不需 LLM）。"""

from __future__ import annotations

import pytest

from studio import runner


@pytest.fixture(autouse=True)
def _runner_tests_default_to_no_sandbox(monkeypatch):
    """核心 runner 測試預設走裸執行；沙箱行為由個別測試明確開啟。"""
    monkeypatch.setattr(runner.config, "SANDBOX_ENABLED", False)


# --- 解析執行指令 -------------------------------------------------------


def test_parse_run_command():
    assert runner.parse_run_command("總結…\n執行指令: python main.py") == "python main.py"
    assert runner.parse_run_command("執行指令：`python bmi.py`") == "python bmi.py"
    assert runner.parse_run_command("沒有宣告") is None


def test_parse_run_command_unwraps_chained_inline_code_spans():
    text = "完成\n執行指令: `echo A`; `echo B`; `echo C`"
    assert runner.parse_run_command(text) == "echo A; echo B; echo C"


def test_parse_run_command_keeps_shell_backtick_substitution():
    text = "完成\n執行指令: echo `printf A`"
    assert runner.parse_run_command(text) == "echo `printf A`"


def test_parse_run_command_ignores_explanatory_tail_after_inline_code():
    text = "完成\n執行指令: `.venv/bin/python -m pytest tests/docs -q`（外層加 timeout 60）"
    assert runner.parse_run_command(text) == ".venv/bin/python -m pytest tests/docs -q"


def test_parse_run_command_handles_extra_leading_markdown_backtick():
    text = "完成\n執行指令: ``.venv/bin/python -m pytest tests/docs -q`（外層加 `timeout 60`）`"
    assert runner.parse_run_command(text) == ".venv/bin/python -m pytest tests/docs -q"


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
    assert runner.resolve_demo_command(tmp_path, None) == "python3 main.py"


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


@pytest.mark.asyncio
async def test_run_command_timeout_kills_grandchild(tmp_path):
    """逾時收尾須殺掉整個 process group，孫程序不可變孤兒存活。

    指令讓 /bin/sh 背景起一個「3 秒後建檔」的 python 孫程序並 wait；timeout=1 觸發收尾。
    只殺直屬 sh（舊行為）→ 孫程序變孤兒、3 秒後仍建出 LEAKED；killpg 整組（修後）→
    孫程序一併被殺，LEAKED 永不出現。明確走非沙箱路徑（sandbox=False）。
    """
    import asyncio as _asyncio

    (tmp_path / "leak.py").write_text(
        "import time, pathlib\ntime.sleep(3)\npathlib.Path('LEAKED').write_text('x')\n",
        encoding="utf-8",
    )
    r = await runner.run_command(tmp_path, "python3 leak.py & wait", timeout=1, sandbox=False)
    assert r.timed_out
    await _asyncio.sleep(3.5)  # 給「若存活」的孫程序足夠時間建檔
    assert not (tmp_path / "LEAKED").exists(), "孫程序逾時後未被殺，變成孤兒繼續執行"


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
async def test_run_command_exec_env_merges_with_parent(tmp_path, monkeypatch):
    monkeypatch.setenv("TI_PARENT_ENV", "parent")
    monkeypatch.setenv("TI_OVERRIDE_ENV", "old")
    r = await runner.run_command_exec(
        tmp_path,
        [
            runner.sys.executable,
            "-c",
            (
                "import os;"
                "print(os.environ.get('TI_PARENT_ENV'));"
                "print(os.environ.get('TI_CHILD_ENV'));"
                "print(os.environ.get('TI_OVERRIDE_ENV'));"
                "print(bool(os.environ.get('PATH')))"
            ),
        ],
        sandbox=False,
        env={"TI_CHILD_ENV": "child", "TI_OVERRIDE_ENV": "new"},
    )
    assert r.ok
    assert r.output.splitlines() == ["parent", "child", "new", "True"]


@pytest.mark.asyncio
async def test_run_command_exec_no_shell_injection(tmp_path):
    """核心安全驗收：argv 內的 shell metacharacters 不被解譯/執行。"""
    payload = "; touch semi `touch backtick` $(touch dollar)\nrm -rf x && touch andand"
    # 把 payload 當單一參數寫進檔案，驗證它原樣保留、且未觸發任何子指令
    r = await runner.run_command_exec(
        tmp_path,
        [
            "python3",
            "-c",
            "import sys,pathlib;pathlib.Path('out.txt').write_text(sys.argv[1])",
            payload,
        ],
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
    r2 = await runner.run_command_exec(tmp_path, ["echo", "hi"], sandbox=False, label="git commit")
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
async def test_run_command_fail_closed(tmp_path, monkeypatch):
    """shell 路徑 fail-closed（PM #1）：沙箱啟用但 bwrap 不存在 → 拒絕執行。

    對稱複製 test_run_command_exec_fail_closed，覆蓋 run_command（shell）路徑。
    同時 monkeypatch SANDBOX_BWRAP 指向不存在路徑「且」SANDBOX_ENABLED=True，
    避免 CI 帶 TI_SANDBOX=0 時走裸跑分支致測試失真。
    """
    monkeypatch.setattr(runner.config, "SANDBOX_BWRAP", "/nonexistent/bwrap")
    monkeypatch.setattr(runner.config, "SANDBOX_ENABLED", True)
    r = await runner.run_command(tmp_path, "touch shouldnt", sandbox=True)
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


# --- 驗收標準 3：功能不退化（正常 commit / 無變更回 None）---------------


@pytest.mark.asyncio
async def test_git_commit_normal_returns_valid_hash(tmp_path):
    """正常訊息成功 commit，回傳短 hash 為 hex 且與實際 HEAD 一致。"""
    assert await runner.git_init(tmp_path) is True
    (tmp_path / "a.txt").write_text("v1")
    h = await runner.git_commit(tmp_path, "正常 commit 訊息")
    assert h and 4 <= len(h) <= 40
    assert all(c in "0123456789abcdef" for c in h), f"非 hex 短 hash：{h!r}"
    head = await runner.run_command_exec(
        tmp_path, ["git", "rev-parse", "--short", "HEAD"], sandbox=False
    )
    assert head.output.strip() == h


@pytest.mark.realgit
@pytest.mark.asyncio
async def test_git_commit_default_forbidden_paths_none_keeps_legacy_return_type(tmp_path):
    """未傳 forbidden_paths 時維持舊介面：成功回短 hash 字串，不回 GitCommitResult。"""
    if not runner._git_available():
        pytest.skip("環境無 git")
    assert await runner.git_init(tmp_path) is True
    (tmp_path / "legacy.txt").write_text("v1")

    result = await runner.git_commit(tmp_path, "legacy return type")

    assert isinstance(result, str)
    assert not isinstance(result, runner.GitCommitResult)
    assert result


@pytest.mark.asyncio
async def test_git_commit_forbidden_paths_uses_acmrt_staged_name_filter(tmp_path, monkeypatch):
    """禁改檢查必須用驗收指定的 staged diff 檔名篩選 argv。"""
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(runner.config, "ENABLE_GIT", True)
    monkeypatch.setattr(runner, "_git_available", lambda: True)
    calls = []

    async def fake_run_command_exec(cwd, argv, **kwargs):
        calls.append(list(argv))
        if argv == ["git", "add", "-A"]:
            return runner.RunOutput("git add", 0, "", False)
        if argv[:3] == ["git", "diff", "--staged"]:
            return runner.RunOutput("git diff --staged", 0, "safe.txt\n", False)
        if argv[:5] == [
            "git",
            "-c",
            f"user.name={runner._GIT_USER_NAME}",
            "-c",
            f"user.email={runner._GIT_USER_EMAIL}",
        ]:
            return runner.RunOutput("git commit", 0, "", False)
        if argv == ["git", "rev-parse", "--short", "HEAD"]:
            return runner.RunOutput("git rev-parse", 0, "abc123\n", False)
        raise AssertionError(f"未預期 git argv：{argv!r}")

    monkeypatch.setattr(runner, "run_command_exec", fake_run_command_exec)

    result = await runner.git_commit(tmp_path, "allowed", forbidden_paths=["docs/"])

    assert isinstance(result, runner.GitCommitResult)
    assert result.commit_hash == "abc123"
    assert [
        "git",
        "diff",
        "--staged",
        "--diff-filter=ACMRT",
        "--name-only",
    ] in calls


@pytest.mark.asyncio
async def test_git_commit_no_change_returns_none_head_unmoved(tmp_path):
    """無變更時回 None，且 HEAD 不移動（不產生空 commit）。"""
    assert await runner.git_init(tmp_path) is True
    (tmp_path / "a.txt").write_text("v1")
    h1 = await runner.git_commit(tmp_path, "first")
    assert h1
    assert await runner.git_commit(tmp_path, "no change") is None
    head = await runner.run_command_exec(
        tmp_path, ["git", "rev-parse", "--short", "HEAD"], sandbox=False
    )
    assert head.output.strip() == h1, "無變更不應移動 HEAD"


@pytest.mark.realgit
@pytest.mark.asyncio
async def test_git_commit_forbidden_paths_blocks_commit_and_returns_violations(tmp_path):
    """帶禁改清單時，staged 命中即不 commit，且回傳違規檔清單。"""
    if not runner._git_available():
        pytest.skip("環境無 git")
    assert await runner.git_init(tmp_path) is True
    (tmp_path / "safe.txt").write_text("base")
    base_hash = await runner.git_commit(tmp_path, "base")
    assert isinstance(base_hash, str)

    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "blocked.md").write_text("blocked")
    (tmp_path / "safe.txt").write_text("changed")
    result = await runner.git_commit(tmp_path, "should be blocked", forbidden_paths=["docs/"])

    assert isinstance(result, runner.GitCommitResult)
    assert result.commit_hash is None
    assert result.forbidden_violations == ["docs/blocked.md"]
    head = await runner.run_command_exec(
        tmp_path, ["git", "rev-parse", "--short", "HEAD"], sandbox=False
    )
    assert head.output.strip() == base_hash, "違規時 HEAD 不應前進"


@pytest.mark.realgit
@pytest.mark.asyncio
async def test_git_commit_forbidden_paths_blocks_deleted_protected_file(tmp_path):
    """刪除禁改檔也算變更，必須擋下且不前進 HEAD。"""
    if not runner._git_available():
        pytest.skip("環境無 git")
    assert await runner.git_init(tmp_path) is True
    (tmp_path / "docs").mkdir()
    protected = tmp_path / "docs" / "protected.md"
    protected.write_text("base")
    base_hash = await runner.git_commit(tmp_path, "base")
    assert isinstance(base_hash, str)

    protected.unlink()
    result = await runner.git_commit(tmp_path, "delete protected", forbidden_paths=["docs/"])

    assert isinstance(result, runner.GitCommitResult)
    assert result.commit_hash is None
    assert result.forbidden_violations == ["docs/protected.md"]
    head = await runner.run_command_exec(
        tmp_path, ["git", "rev-parse", "--short", "HEAD"], sandbox=False
    )
    assert head.output.strip() == base_hash, "刪除禁改檔時 HEAD 不應前進"


@pytest.mark.realgit
@pytest.mark.asyncio
async def test_git_commit_forbidden_paths_blocks_renamed_protected_file_out(tmp_path):
    """把禁改檔移出保護目錄時，來源路徑必須被檢出並擋下。"""
    if not runner._git_available():
        pytest.skip("環境無 git")
    assert await runner.git_init(tmp_path) is True
    (tmp_path / "docs").mkdir()
    protected = tmp_path / "docs" / "protected.md"
    protected.write_text("base")
    base_hash = await runner.git_commit(tmp_path, "base")
    assert isinstance(base_hash, str)

    protected.rename(tmp_path / "moved.md")
    result = await runner.git_commit(tmp_path, "move protected out", forbidden_paths=["docs/"])

    assert isinstance(result, runner.GitCommitResult)
    assert result.commit_hash is None
    assert result.forbidden_violations == ["docs/protected.md"]
    head = await runner.run_command_exec(
        tmp_path, ["git", "rev-parse", "--short", "HEAD"], sandbox=False
    )
    assert head.output.strip() == base_hash, "搬出禁改目錄時 HEAD 不應前進"


@pytest.mark.realgit
@pytest.mark.asyncio
async def test_git_commit_forbidden_paths_allows_non_matching_commit(tmp_path):
    """帶禁改清單但未命中時仍照常 commit，結果物件帶短 hash 與空違規清單。"""
    if not runner._git_available():
        pytest.skip("環境無 git")
    assert await runner.git_init(tmp_path) is True
    (tmp_path / "safe.txt").write_text("v1")

    result = await runner.git_commit(tmp_path, "allowed", forbidden_paths=["docs/"])

    assert isinstance(result, runner.GitCommitResult)
    assert result.commit_hash and result.ok
    assert result.forbidden_violations == []
    head = await runner.run_command_exec(
        tmp_path, ["git", "rev-parse", "--short", "HEAD"], sandbox=False
    )
    assert head.output.strip() == result.commit_hash


@pytest.mark.asyncio
async def test_git_commit_sequential_distinct_hashes(tmp_path):
    """連續多次有變更的 commit 各回不同短 hash。"""
    assert await runner.git_init(tmp_path) is True
    hashes = []
    for i in range(3):
        (tmp_path / f"f{i}.txt").write_text(str(i))
        h = await runner.git_commit(tmp_path, f"commit {i}")
        assert h, f"第 {i} 次 commit 失敗"
        hashes.append(h)
    assert len(set(hashes)) == 3, f"hash 應各異：{hashes}"


@pytest.mark.asyncio
async def test_git_commit_disabled_returns_none(tmp_path, monkeypatch):
    """ENABLE_GIT 關閉時直接回 None（介面契約不變）。"""
    monkeypatch.setattr(runner.config, "ENABLE_GIT", False)
    (tmp_path / "a.txt").write_text("v1")
    assert await runner.git_commit(tmp_path, "msg") is None


# --- 驗收標準 2：四類字面注入 payload，工作目錄不出現 pwned -------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "evil",
    [
        "`touch pwned`",  # backtick 命令替換
        "$(touch pwned)",  # $() 命令替換
        "; touch pwned",  # ; 指令分隔
        "fix\ntouch pwned",  # 換行：第二行為裸指令
    ],
    ids=["backtick", "dollar-paren", "semicolon", "newline"],
)
async def test_git_commit_injection_no_pwned_file(tmp_path, evil):
    """驗收標準 2：commit 訊息含注入向量時，指令不被執行（全樹無 pwned），且 commit 成功。"""
    assert await runner.git_init(tmp_path) is True
    (tmp_path / "f.txt").write_text("data")
    h = await runner.git_commit(tmp_path, evil)
    # 指令未被執行：工作目錄（含所有子目錄）皆無 pwned
    hits = [str(p) for p in tmp_path.rglob("pwned")]
    assert hits == [], f"注入產生檔案：{hits}（payload={evil!r}）"
    # 注入不影響正常功能：commit 仍成功回短 hash
    assert h and len(h) >= 4


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


# --- 驗收標準 4：多行／特殊字元訊息原樣寫入 ----------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "msg",
    [
        "簡單標題",
        "含特殊字元 $VAR `cmd` ;&|<>() \"雙引號\" 'single' \\反斜線",
        "emoji 🚀 與中文標點，。！？ 混排 #hashtag @at %pct",
        "行內 tab\t與 = 號 ~ ^ * ? [ ] { }",
    ],
    ids=["plain", "shell-metachars", "unicode-emoji", "symbols"],
)
async def test_git_commit_singleline_special_chars_verbatim(tmp_path, msg):
    """單行含大量特殊字元，commit message 完全原樣（git -m 不改動單行內容）。"""
    assert await runner.git_init(tmp_path) is True
    (tmp_path / "f.txt").write_text("payload")
    h = await runner.git_commit(tmp_path, msg)
    assert h
    body = await runner.run_command_exec(
        tmp_path, ["git", "log", "-1", "--format=%B"], sandbox=False
    )
    assert body.output.rstrip("\n") == msg, f"訊息未原樣保留：{body.output!r}"


@pytest.mark.asyncio
async def test_git_commit_multiline_body_with_blank_lines_verbatim(tmp_path):
    """多行 body（含中間空行、特殊字元）完整、原樣、逐行保留。"""
    assert await runner.git_init(tmp_path) is True
    (tmp_path / "f.txt").write_text("data")
    msg = (
        "標題：修正 $bug 與 `race`\n"
        "\n"
        "詳述：\n"
        "- 項目 `a` 用 $(cmd)\n"
        "- 項目 \"b\" 與 'c'；含 & | ; 符號\n"
        "\n"
        "結尾段落。"
    )
    h = await runner.git_commit(tmp_path, msg)
    assert h
    body = await runner.run_command_exec(
        tmp_path, ["git", "log", "-1", "--format=%B"], sandbox=False
    )
    # git 僅正規化結尾換行；標題、空行、各行特殊字元一字不差
    assert body.output.rstrip("\n") == msg.rstrip("\n")
    # 逐行確認每一行都完整存在且順序正確
    got_lines = body.output.rstrip("\n").split("\n")
    assert got_lines == msg.split("\n")


@pytest.mark.asyncio
async def test_git_commit_no_message_replace_escaping():
    """驗收標準 1（靜態）：原始碼不殘留 message.replace 跳脫，亦不直接走 shell。"""
    import inspect

    src = inspect.getsource(runner.git_commit)
    assert "message.replace" not in src
    assert "create_subprocess_shell" not in src
    assert "run_command_exec" in src  # 確實改用 exec helper


@pytest.mark.asyncio
async def test_git_commit_routes_through_exec_argv_not_shell(tmp_path, monkeypatch):
    """驗收標準 1（行為）：三步走 run_command_exec(argv list)、不走 shell 路徑，
    且 message 為單一 argv 元素原樣傳遞，未被內插進任何字串。"""
    # .git 已存在 → git_init no-op，排除 git_init 內部的 shell 呼叫干擾
    await runner.run_command_exec(tmp_path, ["git", "init", "-q"], sandbox=False)
    (tmp_path / "f.txt").write_text("x")

    exec_calls: list[list] = []
    shell_calls: list[str] = []
    orig_exec = runner.run_command_exec
    orig_shell = runner.run_command

    async def spy_exec(cwd, argv, **kw):
        exec_calls.append(list(argv))
        return await orig_exec(cwd, argv, **kw)

    async def spy_shell(cwd, command, **kw):
        shell_calls.append(command)
        return await orig_shell(cwd, command, **kw)

    monkeypatch.setattr(runner, "run_command_exec", spy_exec)
    monkeypatch.setattr(runner, "run_command", spy_shell)

    msg = "feat: `touch pwned` $(touch pwned)\n第二行"
    h = await runner.git_commit(tmp_path, msg)
    assert h and len(h) >= 4
    # 三步（add / commit / rev-parse）全走 exec
    assert len(exec_calls) == 3, f"exec 呼叫數={len(exec_calls)}"
    # 完全不走 shell 字串路徑
    assert shell_calls == [], f"不應走 shell：{shell_calls}"
    # message 必為某次 argv 的「單一完整元素」（非內插）
    assert any(msg in call for call in exec_calls), "message 未以單一 argv 元素傳遞"
    # 且任何 argv 元素都不得把 message 內插進更大的字串
    for call in exec_calls:
        for tok in call:
            if tok != msg:
                assert msg not in tok, f"message 被內插進字串：{tok!r}"


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
async def test_git_commit_fail_closed_logs_warning(tmp_path, monkeypatch, caplog):
    """驗收標準 5：fail-closed 分支須 log 一筆 warning，便於排查（設計決策要求）。"""
    import logging

    await runner.run_command_exec(tmp_path, ["git", "init", "-q"], sandbox=False)
    (tmp_path / "f.txt").write_text("data")
    monkeypatch.setattr(runner.config, "SANDBOX_ENABLED", True)
    monkeypatch.setattr(runner.config, "SANDBOX_BWRAP", "/nonexistent/bwrap")
    with caplog.at_level(logging.WARNING, logger="ti.runner"):
        assert await runner.git_commit(tmp_path, "x") is None
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings, "fail-closed 應記一筆 warning"
    assert any("git add" in r.getMessage() for r in warnings), (
        f"warning 應點名失敗步驟：{[r.getMessage() for r in warnings]}"
    )


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
