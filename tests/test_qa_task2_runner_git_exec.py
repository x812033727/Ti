"""QA 驗收：任務 #2「runner.py 固定 git 指令遷移為 run_command_exec」。

對應驗收標準：
- #2：git init/config/clone 改用 run_command_exec（argv list），原始檔不再以 shell
      字串呼叫這些固定指令。
- #3：git_clone 不再對 argv 做 shlex.quote+join，token 遮蔽（output 中 token→***）不變。
- #5（runner 部分）：含 `;`/`&&`/`$()` 的參數被當純文字、單一 argv 元素，不發生注入。

策略：
- git_init 走「真實 git」端到端（sandbox=False），證明 exec 路徑可實際執行並設定身分。
- git_clone 以 spy 攔截 run_command_exec（不碰網路），檢查實際組出的 argv、label、
  token 遮蔽與失敗路徑。
- 雙重保險：把 asyncio.create_subprocess_shell 換成會炸的版本，證明這些函式絕不走 shell。
"""

from __future__ import annotations

import asyncio
import inspect
import re

import pytest
from _repo import REPO_ROOT

from studio import runner
from studio.runner import RunOutput

STUDIO = REPO_ROOT / "studio"


# ---------------------------------------------------------------------------
# 保險絲：本檔任何測試都不得真的開出 shell 子程序。
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _no_shell(monkeypatch):
    async def _boom_shell(*a, **k):
        raise AssertionError("不應呼叫 create_subprocess_shell：git 指令必須走 exec")

    monkeypatch.setattr(asyncio, "create_subprocess_shell", _boom_shell)


class ExecSpy:
    """攔截 run_command_exec：記錄每次的 argv/label/sandbox/timeout，回傳可控假輸出。"""

    def __init__(self, output: str = "", ok: bool = True):
        self.calls: list[dict] = []
        self._output = output
        self._ok = ok

    async def __call__(self, cwd, argv, timeout=None, sandbox=None, label=None, env=None):
        self.calls.append(
            {
                "cwd": cwd,
                "argv": list(argv),
                "timeout": timeout,
                "sandbox": sandbox,
                "label": label,
                "env": env,
            }
        )
        return RunOutput(
            command=label or (argv[0] if argv else ""),
            exit_code=0 if self._ok else 128,
            output=self._output,
            timed_out=False,
        )


# ---------------------------------------------------------------------------
# 驗收 #2：git_init 真實端到端走 exec
# ---------------------------------------------------------------------------
async def test_git_init_real_exec_creates_repo_and_identity(tmp_path):
    """真跑 git_init（sandbox=False）：repo 建立成功，身分用 argv 設定且值正確。"""
    if not runner._git_available():
        pytest.skip("環境無 git")
    ok = await runner.git_init(tmp_path)
    assert ok is True
    assert (tmp_path / ".git").exists(), "git init 未建立 .git"

    async def _cfg(key: str) -> str:
        r = await runner.run_command_exec(
            tmp_path, ["git", "config", "--get", key], timeout=20, sandbox=False
        )
        return r.output.strip()

    # user.name 必須是 "Ti Studio"（無單引號——引號是 shell 產物）。
    assert await _cfg("user.name") == "Ti Studio"
    assert await _cfg("user.email") == "studio@ti.local"
    assert await _cfg("commit.gpgsign") == "false"


async def test_git_init_uses_exec_with_correct_argv(monkeypatch, tmp_path):
    """git_init 四個固定指令皆以 argv 走 run_command_exec、sandbox=False、timeout=20。"""
    spy = ExecSpy(output="", ok=True)
    monkeypatch.setattr(runner, "run_command_exec", spy)

    # run_command（shell）若被呼叫即視為遷移失敗。
    async def _boom_rc(*a, **k):
        raise AssertionError("git_init 不應呼叫 shell 版 run_command")

    monkeypatch.setattr(runner, "run_command", _boom_rc)
    monkeypatch.setattr(runner, "_git_available", lambda: True)

    ok = await runner.git_init(tmp_path)
    assert ok is True

    argvs = [c["argv"] for c in spy.calls]
    assert ["git", "init", "-q"] in argvs
    assert ["git", "config", "user.email", "studio@ti.local"] in argvs
    assert ["git", "config", "user.name", "Ti Studio"] in argvs
    assert ["git", "config", "commit.gpgsign", "false"] in argvs
    # user.name 不得殘留單引號（直接檢查 argv 元素，避免 list repr 自帶引號的誤判）。
    name_argv = next(a for a in argvs if a[:3] == ["git", "config", "user.name"])
    assert name_argv[3] == "Ti Studio", f"user.name 值應為純 'Ti Studio' 無引號：{name_argv[3]!r}"
    assert "'" not in name_argv[3] and '"' not in name_argv[3]
    for c in spy.calls:
        assert c["sandbox"] is False, "git_init 必須顯式 sandbox=False（不可依賴預設）"
        assert c["timeout"] == 20


# ---------------------------------------------------------------------------
# 驗收 #2/#3：git_clone 直接用 parts list 組 argv、無 shlex.quote
# ---------------------------------------------------------------------------
async def test_git_clone_builds_raw_argv_no_quoting(monkeypatch, tmp_path):
    spy = ExecSpy(output="done", ok=True)
    monkeypatch.setattr(runner, "run_command_exec", spy)
    monkeypatch.setattr(runner, "_git_available", lambda: True)

    url = "https://github.com/owner/repo.git"
    await runner.git_clone(url, tmp_path, token=None)

    assert len(spy.calls) == 1
    call = spy.calls[0]
    assert call["argv"] == ["git", "clone", "--depth", "1", url, "."], call["argv"]
    assert call["label"] == "git clone", "label 應固定 'git clone'，不得內插 url"
    assert call["sandbox"] is False
    assert call["timeout"] == 180
    # url 必須是「原樣、單一 argv 元素」——沒有 shlex.quote 加上的外層引號。
    assert call["argv"][-2] == url
    assert "'" not in call["argv"][-2] and '"' not in call["argv"][-2]


async def test_git_clone_branch_appended_as_argv(monkeypatch, tmp_path):
    spy = ExecSpy(output="", ok=True)
    monkeypatch.setattr(runner, "run_command_exec", spy)
    monkeypatch.setattr(runner, "_git_available", lambda: True)

    await runner.git_clone("https://github.com/owner/repo.git", tmp_path, token=None, branch="main")
    argv = spy.calls[0]["argv"]
    assert "--branch" in argv and argv[argv.index("--branch") + 1] == "main"


@pytest.mark.parametrize(
    "payload",
    [
        "https://github.com/owner/repo.git; rm -rf /",
        "https://github.com/owner/repo.git && curl evil.sh",
        "https://github.com/owner/repo.git$(whoami)",
        "https://github.com/owner/`id`/repo.git",
    ],
)
async def test_git_clone_metachars_are_single_literal_argv(monkeypatch, tmp_path, payload):
    """含 ; / && / $() / `` 的 url 必須當純文字、塞進單一 argv 元素，不被拆解。"""
    spy = ExecSpy(output="", ok=True)
    monkeypatch.setattr(runner, "run_command_exec", spy)
    monkeypatch.setattr(runner, "_git_available", lambda: True)

    await runner.git_clone(payload, tmp_path, token=None)
    argv = spy.calls[0]["argv"]
    # payload 整串就是倒數第二個元素，沒有被 shell 切開成多個 token。
    assert argv[-2] == payload, argv
    assert argv[-1] == "."
    # argv 不會因 metachar 而多出元素（固定 6 個：git clone --depth 1 <url> .）
    assert len(argv) == 6


# ---------------------------------------------------------------------------
# 驗收 #3：token 遮蔽維持不變（含失敗路徑）
# ---------------------------------------------------------------------------
async def test_git_clone_masks_token_in_output(monkeypatch, tmp_path):
    token = "ghp_SECRET_TOKEN_123"
    # 模擬輸出回吐含 token 的 authed url（成功也可能 echo）。
    leaked = f"Cloning into '.'...\nremote: https://x-access-token:{token}@github.com/o/r"
    spy = ExecSpy(output=leaked, ok=True)
    monkeypatch.setattr(runner, "run_command_exec", spy)
    monkeypatch.setattr(runner, "_git_available", lambda: True)

    url = "https://github.com/o/r.git"
    result = await runner.git_clone(url, tmp_path, token=token)
    assert token not in result.output, "token 必須被遮蔽"
    assert "***" in result.output
    # command 無條件重設為原始 url，不含 token。
    assert result.command == f"git clone {url}"
    assert token not in result.command


async def test_git_clone_masks_token_on_failure_path(monkeypatch, tmp_path):
    """失敗路徑：clone 失敗 stderr 回吐含 token 的 url，合併輸出下 *** 仍生效。"""
    token = "ghp_FAIL_TOKEN_999"
    leaked = (
        f"fatal: could not read from "
        f"https://x-access-token:{token}@github.com/o/r.git\n"
        "remote: Repository not found."
    )
    spy = ExecSpy(output=leaked, ok=False)
    monkeypatch.setattr(runner, "run_command_exec", spy)
    monkeypatch.setattr(runner, "_git_available", lambda: True)

    url = "https://github.com/o/r.git"
    result = await runner.git_clone(url, tmp_path, token=token)
    assert result.ok is False
    assert token not in result.output, "失敗路徑 token 仍須遮蔽"
    assert "***" in result.output
    assert token not in result.command


async def test_git_clone_label_carries_no_token(monkeypatch, tmp_path):
    """傳給 run_command_exec 的 label 不得含 token（避免寫進日誌）。"""
    token = "ghp_LABEL_LEAK"
    spy = ExecSpy(output="", ok=True)
    monkeypatch.setattr(runner, "run_command_exec", spy)
    monkeypatch.setattr(runner, "_git_available", lambda: True)

    await runner.git_clone("https://github.com/o/r.git", tmp_path, token=token)
    for c in spy.calls:
        assert token not in str(c["label"])
        assert token not in str(c["argv"][:-2]), "前置 argv（label 來源附近）不應含 token"


# ---------------------------------------------------------------------------
# 驗收 #2：原始碼層級——這些函式不再以 shell run_command 跑固定 git 指令
# ---------------------------------------------------------------------------
def test_source_no_shell_run_command_in_git_funcs():
    src = inspect.getsource(runner.git_init) + inspect.getsource(runner.git_clone)
    # 不得出現 shell 版 run_command( 呼叫（run_command_exec 不算）。
    shell_hits = re.findall(r"(?<!_exec)\brun_command\(", src)
    assert not shell_hits, f"git_init/git_clone 仍殘留 shell run_command：{len(shell_hits)} 處"
    assert "run_command_exec(" in src


def test_source_git_clone_no_shlex_quote_join():
    src = inspect.getsource(runner.git_clone)
    assert "shlex.quote" not in src, "git_clone 不應再對 argv 做 shlex.quote"
    assert ".join(" not in src, "git_clone 不應再 join argv 回字串"
    # 仍保有 token 遮蔽與 command 覆寫。
    assert 'replace(token, "***")' in src
    assert 'result.command = "git clone "' in src
