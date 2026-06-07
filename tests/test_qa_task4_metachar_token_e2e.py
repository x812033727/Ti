"""QA 驗收：任務 #4「為已遷移呼叫端補測試——metacharacter 純文字 + token 遮蔽」。

既有測試（task2 / task3 / task4_autopilot_gate / test_clone）已從 spy 與部分端到端角度
覆蓋遷移後呼叫端。本檔補齊兩個「真實 subprocess」層級的缺口，讓任務 #4 的兩項要求都有
不靠 spy 的硬證據：

1. metacharacter 純文字：經真實 run_command_exec（遷移後共用執行路徑）傳入 ;/&&/$()/``，
   參數放在多個 argv 位置都不被解析、不產生副作用。
2. token 遮蔽：對「真實 subprocess 合併 stdout+stderr 後實際捕獲的輸出」套用 git_clone
   同款遮蔽，token → ***（成功與失敗路徑皆驗），證明遮蔽契約對真實輸出成立，
   而非僅對手工捏造字串成立。

另含跨呼叫端整合守門：所有遷移呼叫端（git_init / git_clone / _gate_tests）皆走 exec、
無一殘留 shell run_command。
"""

from __future__ import annotations

import inspect
import re
import sys

import pytest

from studio import autopilot, runner


# ---------------------------------------------------------------------------
# 1. metacharacter 純文字（真實 exec、多 argv 位置、零副作用）
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_metachars_literal_across_argv_positions(tmp_path):
    """把含 ;/&&/$()/`` 的 payload 放進多個 argv 元素，真實 exec 後：
    - 每個 payload 原樣傳到程式（純文字）；
    - 任何被注入的子指令都沒有執行（哨兵檔皆不存在）。
    """
    payloads = [
        "; touch INJECT_semi",
        "&& touch INJECT_and",
        "$(touch INJECT_dollar)",
        "`touch INJECT_backtick`",
    ]
    # 程式把每個 argv 參數寫成獨立檔案，回頭比對內容是否原樣。
    prog = (
        "import sys,pathlib\n"
        "for i,a in enumerate(sys.argv[1:]):\n"
        "    pathlib.Path(f'arg{i}.txt').write_text(a)\n"
    )
    r = await runner.run_command_exec(
        tmp_path, [sys.executable, "-c", prog, *payloads], timeout=60, sandbox=False
    )
    assert r.ok, r.output
    # 沒有任何注入指令被執行。
    for ghost in ("INJECT_semi", "INJECT_and", "INJECT_dollar", "INJECT_backtick"):
        assert not (tmp_path / ghost).exists(), f"metacharacter 被解析、注入指令執行了：{ghost}"
    # 每個 payload 原樣抵達程式。
    for i, p in enumerate(payloads):
        assert (tmp_path / f"arg{i}.txt").read_text() == p


@pytest.mark.asyncio
async def test_metachars_no_glob_expansion(tmp_path):
    """exec 不做 glob 展開：`*` 原樣當文字，不被展開成檔名清單。"""
    (tmp_path / "a.py").write_text("", encoding="utf-8")
    (tmp_path / "b.py").write_text("", encoding="utf-8")
    prog = "import sys,pathlib;pathlib.Path('seen.txt').write_text('|'.join(sys.argv[1:]))"
    r = await runner.run_command_exec(
        tmp_path, [sys.executable, "-c", prog, "*.py"], timeout=60, sandbox=False
    )
    assert r.ok, r.output
    assert (tmp_path / "seen.txt").read_text() == "*.py", "glob 不應被展開"


# ---------------------------------------------------------------------------
# 2. token 遮蔽作用在「真實合併輸出」上（成功 + 失敗路徑）
# ---------------------------------------------------------------------------
def _mask(output: str, token: str) -> str:
    """git_clone 採用的同款遮蔽（runner.git_clone: output.replace(token, '***')）。"""
    return output.replace(token, "***")


@pytest.mark.asyncio
async def test_token_masking_on_real_merged_output_success(tmp_path):
    """真實 exec 把含 token 的字串同時寫 stdout 與 stderr，合併捕獲後遮蔽成立。"""
    token = "ghp_REAL_SECRET_abcdef123456"
    prog = (
        "import sys\n"
        f"sys.stdout.write('clone url https://x-access-token:{token}@github.com/o/r\\n')\n"
        f"sys.stderr.write('remote echo {token}\\n')\n"
    )
    r = await runner.run_command_exec(
        tmp_path, [sys.executable, "-c", prog], timeout=60, sandbox=False
    )
    assert r.ok
    assert token in r.output, "前置條件：真實輸出確實含 token（stdout+stderr 已合併）"
    masked = _mask(r.output, token)
    assert token not in masked, "遮蔽後 token 不得殘留於真實合併輸出"
    assert "***" in masked


@pytest.mark.asyncio
async def test_token_masking_on_real_merged_output_failure(tmp_path):
    """失敗路徑（非零退出）：stderr 回吐含 token 的 url，合併輸出下遮蔽仍生效。"""
    token = "ghp_FAIL_SECRET_zzz999"
    prog = (
        "import sys\n"
        f"sys.stderr.write('fatal: could not read "
        f"https://x-access-token:{token}@github.com/o/r.git\\n')\n"
        "sys.exit(128)\n"
    )
    r = await runner.run_command_exec(
        tmp_path, [sys.executable, "-c", prog], timeout=60, sandbox=False
    )
    assert r.ok is False and r.exit_code == 128
    assert token in r.output, "前置條件：失敗時 stderr 的 token 已被合併捕獲"
    masked = _mask(r.output, token)
    assert token not in masked
    assert "***" in masked


@pytest.mark.asyncio
async def test_git_clone_real_masking_contract_matches_helper(tmp_path, monkeypatch):
    """釘住 git_clone 真的用 output.replace(token,'***')：餵真實洩漏輸出給 spy，
    比對 git_clone 產出與本檔 _mask 助手一致（遮蔽邏輯未走樣）。"""
    token = "ghp_CONTRACT_SECRET_001"
    leaked = f"line1 {token}\nline2 https://x-access-token:{token}@h/r\n{token}"

    async def spy(cwd, argv, timeout=None, sandbox=None, label=None):
        return runner.RunOutput(command=label or argv[0], exit_code=0, output=leaked, timed_out=False)

    monkeypatch.setattr(runner, "run_command_exec", spy)
    monkeypatch.setattr(runner, "_git_available", lambda: True)
    result = await runner.git_clone("https://github.com/o/r.git", tmp_path, token=token)
    assert result.output == _mask(leaked, token)
    assert token not in result.output and "***" in result.output


# ---------------------------------------------------------------------------
# 3. 跨呼叫端整合守門：所有遷移呼叫端皆走 exec，無殘留 shell run_command
# ---------------------------------------------------------------------------
def test_all_migrated_callers_use_exec_only():
    funcs = [runner.git_init, runner.git_clone, autopilot._gate_tests]
    for fn in funcs:
        src = inspect.getsource(fn)
        shell_hits = re.findall(r"(?<!_exec)\brun_command\(", src)
        assert not shell_hits, f"{fn.__name__} 仍殘留 shell run_command：{len(shell_hits)} 處"
        assert "run_command_exec(" in src, f"{fn.__name__} 未使用 run_command_exec"
