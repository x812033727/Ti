"""QA 任務 #5（PM #6 不碰網路）獨立驗證：

證明 test_clone.py 的「不碰網路」是真有效力，而非靠運氣：
1. 絆線實證：在與 test_clone.py 相同的 autouse 保險絲下，若繞過 spy 走「真實
   run_command」呼叫 git_clone，必須在子程序建立處炸開（RuntimeError），
   而非默默連網 → 證明任何「真跑 clone」都會被擋。
2. spy 路徑零副作用：以 spy 攔截時，tmp_path 不得出現任何真實 clone 痕跡
   （無 .git、目錄維持空），且絕無子程序被啟動。
3. 結構防呆：掃描 test_clone.py，凡呼叫 git_clone 的測試函式都必須吃 clone_spy。
"""

from __future__ import annotations

import asyncio
import inspect
import re
from pathlib import Path

import pytest

from studio import runner


@pytest.fixture(autouse=True)
def _forbid_real_subprocess(monkeypatch):
    """複製 test_clone.py 的保險絲：本檔全程禁止啟動真實子程序。"""

    async def _boom(*args, **kwargs):
        raise RuntimeError("test forbids spawning a real subprocess (no network)")

    monkeypatch.setattr(asyncio, "create_subprocess_shell", _boom)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _boom)


@pytest.mark.asyncio
async def test_real_clone_path_is_tripwired(monkeypatch, tmp_path):
    """繞過 spy 走真實 run_command：git_clone 必被絆線擋下、絕不連網。

    git_clone(sandbox=False) → run_command → asyncio.create_subprocess_shell，
    該函式已被換成 _boom；例外無人攔截，故 git_clone 會拋 RuntimeError。
    """
    monkeypatch.setattr(runner, "_git_available", lambda: True)
    with pytest.raises(RuntimeError, match="no network"):
        await runner.git_clone(
            "https://github.com/owner/repo.git", tmp_path, token=None
        )
    # 絆線在「建立子程序」當下就炸 → tmp_path 不會有任何 clone 痕跡。
    assert not (tmp_path / ".git").exists()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_spy_path_has_zero_side_effects(monkeypatch, tmp_path):
    """spy 攔截路徑：tmp_path 全程零副作用，且未啟動任何子程序。"""
    calls = []

    async def spy(cwd, command, timeout=None, sandbox=None):
        calls.append(command)
        return runner.RunOutput("git clone (fake)", 0, "ok", False)

    monkeypatch.setattr(runner, "run_command", spy)
    monkeypatch.setattr(runner, "_git_available", lambda: True)

    result = await runner.git_clone(
        "https://github.com/owner/repo.git", tmp_path, token="ghp_x", branch="main"
    )

    # 走攔截：run_command 被呼叫，但 tmp_path 沒有任何實體 clone 結果。
    assert calls, "spy 應被呼叫"
    assert not (tmp_path / ".git").exists()
    assert list(tmp_path.iterdir()) == []
    assert result.command == "git clone https://github.com/owner/repo.git"


def test_all_git_clone_tests_use_clone_spy():
    """結構防呆：test_clone.py 內凡呼叫 git_clone 的測試都必須依賴 clone_spy。

    釘住「不碰網路」設計：任何呼叫 git_clone 的測試必須走「攔截」(clone_spy) 或
    「絆線」(pytest.raises(RuntimeError) 靠 autouse 保險絲擋下真實子程序) 二者之一。
    若有測試既不吃 clone_spy、又不靠絆線就呼叫 git_clone（恐連網），本測試立刻變紅。
    """
    src = Path(__file__).with_name("test_clone.py").read_text(encoding="utf-8")
    # 切出每個 test 函式的原始碼區塊（粗略以 def test_ 為界）。
    funcs = re.split(r"\nasync def |\ndef ", "\n" + src)
    offenders = []
    for block in funcs:
        head = block.splitlines()[0] if block else ""
        if not head.startswith("test_"):
            continue
        name = head.split("(")[0]
        if "runner.git_clone(" not in block:
            continue
        via_spy = "clone_spy" in block
        # 絆線測試：刻意走真實路徑但用 pytest.raises(RuntimeError) 證明被保險絲擋下。
        via_tripwire = "pytest.raises(RuntimeError" in block
        if not (via_spy or via_tripwire):
            offenders.append(name)
    assert not offenders, f"這些 git_clone 測試未走 clone_spy／絆線（恐連網）：{offenders}"


def test_clone_spy_fixture_blocks_real_run_command():
    """確認 clone_spy fixture 確實 monkeypatch 掉 runner.run_command（攔截就位）。"""
    src = Path(__file__).with_name("test_clone.py").read_text(encoding="utf-8")
    assert 'monkeypatch.setattr(runner, "run_command", spy)' in src
    assert 'monkeypatch.setattr(asyncio, "create_subprocess_shell"' in src
    assert 'monkeypatch.setattr(asyncio, "create_subprocess_exec"' in src
