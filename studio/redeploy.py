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

from . import autonomy, config, deploy, notify, runner


def _redact(text: str) -> str:
    """遮蔽輸出中的 GitHub token，避免任何回傳／log 外洩秘密。"""
    token = config.GITHUB_TOKEN
    if token and text:
        text = text.replace(token, "***")
    return text


async def pull_main() -> runner.RunOutput:
    """在主 repo（PROJECT_ROOT）拉取最新 main（fast-forward only），回傳執行結果。

    指令為固定字串、無 shell 語法，走 argv 式 run_command_exec（不經 /bin/sh），
    與 subprocess 遷移清冊的 (a) 類基準一致。
    """
    return await runner.run_command_exec(
        config.PROJECT_ROOT,
        ["git", "pull", "--ff-only"],
        timeout=120,
        sandbox=False,
        label="git pull",
    )


async def import_smoke() -> runner.RunOutput:
    """exec 前的安全健檢：用子程序 import 服務進入點，確認新碼至少能 import。

    擋掉「pull 進語法/import 壞掉的 main → os.execv 進壞碼 → 服務起不來且無回滾」這條路
    （deploy.redeploy() 有 health+rollback，但本路徑走 os.execv 沒有，故先 import 驗一道）。
    """
    return await runner.run_command_exec(
        config.PROJECT_ROOT,
        [sys.executable, "-c", "import studio.server"],
        timeout=60,
        sandbox=False,
        label="import smoke",
    )


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


async def redeploy(*, restart: bool = True, governance: dict | None = None) -> dict:
    """拉取最新 main，成功後（restart=True）排程自我重啟。

    回傳 dict：{ok, pulled, restarting, detail}。任何失敗皆不丟例外。
    """
    if autonomy.policy_exists(autonomy.CORE_PROJECT_ID):
        policy = autonomy.load_policy(autonomy.CORE_PROJECT_ID)
        evidence = dict(governance or {})
        evidence.setdefault("risk", "high-reversible" if policy["stage"] >= 3 else "medium")
        decision = autonomy.evaluate_operation(
            autonomy.CORE_PROJECT_ID,
            "deploy",
            evidence,
            approvals=evidence.get("approval_verdicts") or [],
            human_approved=bool(evidence.get("human_approved")),
            source_sha=str(evidence.get("source_sha") or "unknown"),
        )
        if not decision["external_write_allowed"]:
            detail = (
                "shadow 模式禁止實際重新部署"
                if decision["allowed"]
                else "重新部署前自治政策拒絕：" + ",".join(decision["reasons"])
            )
            notify.send_bg("policy_violation", detail, project_id=autonomy.CORE_PROJECT_ID)
            return {"ok": False, "pulled": False, "restarting": False, "detail": detail}

    # 與 autopilot / autodeploy timer 的 deploy.redeploy() 共用同一把 flock，避免並行部署互撞。
    with deploy._deploy_lock() as acquired:
        if not acquired:
            return {
                "ok": False,
                "pulled": False,
                "restarting": False,
                "detail": "另一個部署進行中，請稍後再試",
            }
        pull = await pull_main()
        detail = _redact(pull.output).strip()
        result = {"ok": pull.ok, "pulled": pull.ok, "restarting": False, "detail": detail}
        if not pull.ok:
            result["detail"] = "git pull 失敗：" + detail
            return result
        if restart:
            smoke = await import_smoke()
            if not smoke.ok:
                # 新碼 import 失敗：不重啟，服務維持舊版（已 pull 的新檔等人工修正再起）。
                result["ok"] = False
                result["detail"] = (
                    "已拉取最新 main，但新版 import 檢查失敗，已取消重啟（服務維持運行中的舊版）：\n"
                    + _redact(smoke.output)[-800:]
                )
                return result
            result["restarting"] = True
            result["detail"] = (
                "已拉取最新 main 且 import 檢查通過，服務即將重啟以套用新版程式碼"
                "（進行中的工作／連線會中斷）…"
            )
            schedule_restart()
        else:
            result["detail"] = "已拉取最新 main（未重啟）"
        return result
