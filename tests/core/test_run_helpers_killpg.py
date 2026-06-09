"""回歸測試：autopilot._run / deploy._run 逾時時殺整個 process group，孫程序不留孤兒。

與 runner._finalize_proc 同一類修復——兩處 _run 原本逾時只 proc.kill() 直屬子程序，
/bin/sh 背景起的工作程序（孫程序）會變孤兒繼續執行。現皆改 start_new_session=True
＋ runner.kill_process_group（killpg 整組）。
"""

from __future__ import annotations

import asyncio

import pytest

from studio import autopilot, deploy


@pytest.mark.asyncio
@pytest.mark.parametrize("run_fn", [autopilot._run, deploy._run], ids=["autopilot", "deploy"])
async def test_run_helper_timeout_kills_grandchild(run_fn, tmp_path):
    (tmp_path / "leak.py").write_text(
        "import time, pathlib\ntime.sleep(2)\npathlib.Path('LEAKED').write_text('x')\n",
        encoding="utf-8",
    )
    # sh 背景起「2 秒後建檔」的 python 孫程序並 wait；timeout=1 觸發收尾。
    rc, _out = await run_fn(["sh", "-c", "python3 leak.py & wait"], cwd=str(tmp_path), timeout=1)
    assert rc == -1  # 逾時碼
    await asyncio.sleep(3)  # 給「若存活」的孫程序足夠時間建檔
    assert not (tmp_path / "LEAKED").exists(), "孫程序逾時後未被殺，變成孤兒繼續執行"
