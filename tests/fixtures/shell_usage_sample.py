"""掃描測試樣本（故意含 shell 注入面，僅供 scan_shell_usage.sh 驗證命中用，勿在正式碼引用）。"""

import asyncio
import subprocess


def via_subprocess_shell_true(cmd: str) -> None:
    # 應由 Ruff S602/S604 命中。
    subprocess.run(cmd, shell=True)


async def via_create_subprocess_shell(cmd: str) -> None:
    # 應由 grep step 命中（S 規則不抓）。
    proc = await asyncio.create_subprocess_shell(cmd)
    await proc.wait()
