"""重新佈署重啟：把主 repo 拉到最新 main，再讓服務行程自我重啟，讓新程式碼生效。

形成自我改進閉環的最後一哩：成果合併進主 repo 後，呼叫此處即可上線。
- `pull_main()`：在專案根目錄（主 repo）執行 git pull（純 IO，token 已遮蔽）。
- `schedule_restart()`：延遲後以 os.execv 重新 exec 自己，無需外部 process manager。
- `redeploy()`：組合上述兩者，回傳可序列化的結果 dict（不含明文 token）。
後備：見 scripts/redeploy.sh（純 shell 版本，供外部排程／人工使用）。
"""

from __future__ import annotations

import asyncio
import os
import sys

from . import config, runner


def _redact(text: str) -> str:
    """遮蔽輸出中的 GitHub token，避免任何回傳／log 外洩秘密。"""
    token = config.GITHUB_TOKEN
    if token and text:
        text = text.replace(token, "***")
    return text


async def pull_main() -> runner.RunOutput:
    """在主 repo（PROJECT_ROOT）拉取最新 main（fast-forward only），回傳執行結果。"""
    return await runner.run_command(config.PROJECT_ROOT, "git pull --ff-only", timeout=120)


def _do_restart() -> None:  # pragma: no cover - 真的會替換掉行程，測試以 monkeypatch 取代
    """以原始啟動參數重新 exec 自己，達成自我重啟。

    用 `sys.argv` 保留實機真正的啟動方式（host/port/wrapper 等），避免 execv 後
    參數遺失而起在錯的埠或起不來。
    """
    os.execv(sys.executable, [sys.executable, *sys.argv])


def schedule_restart(delay: float = 0.5) -> None:
    """排程延遲重啟，讓當前 HTTP 回應能先送出再替換行程。"""
    loop = asyncio.get_running_loop()
    loop.call_later(delay, _do_restart)


async def redeploy(*, restart: bool = True) -> dict:
    """拉取最新 main，成功後（restart=True）排程自我重啟。

    回傳 dict：{ok, pulled, restarting, detail}。任何失敗皆不丟例外。
    """
    pull = await pull_main()
    detail = _redact(pull.output).strip()
    result = {"ok": pull.ok, "pulled": pull.ok, "restarting": False, "detail": detail}
    if not pull.ok:
        result["detail"] = "git pull 失敗：" + detail
        return result
    if restart:
        result["restarting"] = True
        result["detail"] = (
            "已拉取最新 main，服務即將重啟以套用新版程式碼（進行中的工作／連線會中斷）…"
        )
        schedule_restart()
    else:
        result["detail"] = "已拉取最新 main（未重啟）"
    return result
