"""ti-autodeploy 的納管版本（完成率第三輪修法二A 收尾）：輪詢 origin/<branch>，偵測到
新 commit 且當下無進行中討論時，重用 studio.deploy.redeploy()（fetch→reset→重裝→restart
→健康檢查→失敗回滾）讓新碼上線。由 systemd timer（deploy/ti-autodeploy.timer）週期觸發。

為什麼搬進 repo：原 /usr/local/sbin/ti-autodeploy.py 不受版控——pip install -e . 不會更新
它，任何邏輯修正都要一次性人工運維、無法被 autopilot 自動迭代，也沒有測試。搬進 studio/
後與其他模組同生命週期（合併即生效、可測）；sbin 舊腳本在 unit 檔切換前照跑，行為不變。

新增的可觀測性：因討論進行中而延後時，寫 <AUTOPILOT_STATE_DIR>/autodeploy-deferred.json
（{first_deferred_at, deferrals, remote}）；成功部署或無 drift 時刪除。deploy.drift_stats()
（/api/autopilot 的 deploy 欄）會透傳此檔——「部署飢餓」從此看板可判，不必翻 journal。
純觀測，不改部署行為。
"""

from __future__ import annotations

import asyncio
import json
import sys
import time

from . import config, deploy, history


def _deferred_path():
    return config.AUTOPILOT_STATE_DIR / "autodeploy-deferred.json"


def _note_deferred(remote: str) -> None:
    """累計「有討論延後」觀測檔（換了目標 commit 重計；壞檔視同不存在；寫失敗不擋部署輪）。"""
    path = _deferred_path()
    data = {"first_deferred_at": time.time(), "deferrals": 0, "remote": remote}
    try:
        prev = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(prev, dict) and prev.get("remote") == remote:
            data = prev
    except (OSError, ValueError):
        pass
    data["deferrals"] = int(data.get("deferrals", 0)) + 1
    data["remote"] = remote
    try:
        config.AUTOPILOT_STATE_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def _clear_deferred() -> None:
    try:
        _deferred_path().unlink(missing_ok=True)
    except OSError:
        pass


async def run_once() -> int:
    """一輪 autodeploy：無 drift＝0；延後＝0；部署成功＝0；fetch/部署失敗＝1。"""
    deploy_dir = str(config.AUTOPILOT_DEPLOY_DIR)
    branch = config.AUTOPILOT_BRANCH

    rc, out = await deploy._run(["git", "fetch", "origin", branch], cwd=deploy_dir, timeout=120)
    if rc != 0:
        print(f"[autodeploy] git fetch 失敗：{out[-300:]}")
        return 1

    local = await deploy.current_head(deploy_dir)
    rc, remote = await deploy._run(
        ["git", "rev-parse", f"origin/{branch}"], cwd=deploy_dir, timeout=30
    )
    remote = remote.strip()
    if not remote or local == remote:
        _clear_deferred()
        print("[autodeploy] 無新 commit，略過")
        return 0

    # 只把「真正進行中」的討論當作 busy；卡在 running 但 stale（崩潰沒收尾）的不算，
    # 避免死 session 永久擋住部署（與 autopilot._wait_until_idle 共用同一判定）。
    running = history.busy_sessions(config.DEPLOY_STALE_AFTER)
    if running:
        _note_deferred(remote)
        print(f"[autodeploy] 有 {len(running)} 場進行中討論，延後到下一輪")
        return 0

    print(f"[autodeploy] 偵測到新 commit {local[:8]} → {remote[:8]}，開始重佈…")
    ok, msg = await deploy.redeploy()
    print(f"[autodeploy] {msg}")
    if ok:
        _clear_deferred()
    return 0 if ok else 1


def main() -> int:
    return asyncio.run(run_once())


if __name__ == "__main__":
    sys.exit(main())
