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

from . import autonomy, config, deploy, history, notify


def _deferred_path():
    return config.AUTOPILOT_STATE_DIR / "autodeploy-deferred.json"


def _note_deferred(remote: str, *, reason: str = "busy_sessions") -> bool:
    """累計同一 remote+延後原因，回傳是否為這組身分的第一次。"""
    path = _deferred_path()
    data = {
        "first_deferred_at": time.time(),
        "deferrals": 0,
        "remote": remote,
        "reason": reason,
    }
    try:
        prev = json.loads(path.read_text(encoding="utf-8"))
        previous_reason = (
            str(prev.get("reason") or "busy_sessions") if isinstance(prev, dict) else ""
        )
        if isinstance(prev, dict) and prev.get("remote") == remote and previous_reason == reason:
            data = prev
    except (OSError, ValueError):
        pass
    data["deferrals"] = int(data.get("deferrals", 0)) + 1
    data["remote"] = remote
    data["reason"] = reason
    try:
        config.AUTOPILOT_STATE_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass
    return data["deferrals"] == 1


def _clear_deferred() -> None:
    try:
        _deferred_path().unlink(missing_ok=True)
    except OSError:
        pass


async def run_once() -> int:
    """一輪 autodeploy：無 drift＝0；延後＝0；部署成功＝0；fetch/部署失敗＝1。"""
    deploy_dir = str(config.AUTOPILOT_DEPLOY_DIR)
    branch = config.AUTOPILOT_BRANCH

    # deploy_dir 是 origin 單向鏡像；force refspec 避免並行 fetch 的 ref CAS 競爭。
    rc, out = await deploy._run(
        ["git", "fetch", "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}"],
        cwd=deploy_dir,
        timeout=120,
    )
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

    # 納管後的 deploy 是 high-reversible：必須綁定 run、exact merge SHA、dry-run/
    # backup/rollback 證據與雙 provider verdict。timer 天生沒有這些證據，不得
    # 每兩分鐘呼叫 deploy.redeploy() 製造一筆假的高風險「操作」。對同一 SHA
    # 只在首次留 audit+外部通知，之後保留 deferred 計數供看板觀測。
    if autonomy.policy_exists(autonomy.CORE_PROJECT_ID):
        reason = "governance_evidence_required"
        first = _note_deferred(remote, reason=reason)
        if first:
            try:
                autonomy.emit_event(
                    "policy_violation",
                    project_id=autonomy.CORE_PROJECT_ID,
                    source_sha=local or "unknown",
                    outcome="autodeploy_governance_deferred",
                    severity="warning",
                    payload={"remote_source_sha": remote, "reason": reason},
                )
            except Exception as exc:  # fail-closed：audit 無法落檔不能當成正常延後
                print(f"[autodeploy] 治理延後 audit 寫入失敗：{type(exc).__name__}")
                return 1
            notify.send_bg(
                "policy_violation",
                "autodeploy 偵測到未綁定治理證據的 main drift，已 fail-closed 延後",
                project_id=autonomy.CORE_PROJECT_ID,
                remote_sha=remote[:12],
            )
        print(
            f"[autodeploy] 偵測到 {local[:8]} → {remote[:8]}，"
            "但納管 deploy 需 exact SHA 與審查證據，已延後"
        )
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
