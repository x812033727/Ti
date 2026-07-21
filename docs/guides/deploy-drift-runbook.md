# 部署漂移 operator 手冊（deploy drift runbook）

> 場景：`main` 已前進（外部合併／人工 gh merge），但線上服務仍跑舊碼。
> 來源：2026-07-21 事故——#520/#521/#522 合併後 10 小時無法上線，最終 operator
> 只能繞到 OS 層手動部署（治理層零留痕）。本手冊把當時的排查與正規處置固化。

## 一、怎麼發現

- **「需要你」收件匣**：部署漂移卡（remote SHA、延後原因、累計輪數）。
- `GET /api/autopilot`（authed）的 `deploy` 欄：`drift_stats()` 快照
  （disk_head／origin_head／behind／deferred）。
- journal：`journalctl -u ti-autodeploy` 每 2 分鐘一筆延後原因。

## 二、為什麼會卡（#504 治理層）

政策檔（`autopilot/autonomy/policies/ti-studio.json`）存在時，所有部署路徑
（autodeploy timer、任務邊界重佈、`/api/redeploy`、`deploy.redeploy()`）都要過
`autonomy.evaluate_operation` 的 deploy 關卡：high-reversible 操作必須具備

1. rollback 四證據：`dry_run`／`backup`／`verified`／`scope_limit`
   （來源＝28 天內平台驗證過的 rollback drill，`autonomy.deployment_rollback_evidence`）；
2. 雙 provider 對 **exact diff＋evidence** 的獨立核可（`autonomy_review.review`，
   兩者皆 `approve` 才過；`escalate`＝升級給人）。

autopilot 自己合併的任務在 merge 時就組好證據、部署自然放行；**外部合併的
commit 沒人幫它組證據**，timer 只能 fail-closed 延後。

## 三、正規處置（帶證據的納管部署）

參考腳本範式（7-21 實跑過）：

```python
# 於 /opt/ti 以 .venv/bin/python 執行;全程 fail-closed,雙審不過即中止。
from studio import autonomy, autonomy_review, deploy
# 1) diff:  git diff --binary <deployed>..<target>  → sha256=diff_sha
# 2) rollback: autonomy.deployment_rollback_evidence(CORE_PROJECT_ID, <deployed>)
#    (四鍵必須全 True;否則先跑 rollback drill: POST /api/autonomy/rollback-drills)
# 3) evidence_text: {"execution": {CI check-runs 原始結論}, "rollback": ...,
#    "previous_deployed_revision": <deployed>,
#    "mechanism": "builtin_redeploy_last_good_with_health_and_blackbox"}
#    (ensure_ascii=False, sort_keys=True) → sha256=evidence_sha
# 4) approvals = await autonomy_review.review(cwd, diff_text, evidence_text,
#    diff_sha, evidence_sha, session_id=...)
# 5) 兩票皆 approve → await deploy.redeploy(governance={
#    "risk": "high-reversible", "diff_sha": ..., "evidence_sha": ...,
#    "rollback": ..., "approval_verdicts": approvals, "human_approved": False,
#    "run_id": ..., "task_id": "manual",
#    "source_sha": <deployed>, "expected_source_sha": <target>})
```

要點：

- `source_sha`＝**部署前基線**（現在線上的 SHA）、`expected_source_sha`＝目標；
  redeploy 會重驗兩者（`deployed_sha_drift`／`source_sha_drift` 直接熔斷並 trip brake）。
- `deploy.redeploy()` 自帶 health check＋黑盒探針＋失敗自動回滾——正規路徑
  永遠優先於手動 OS 操作。
- redeploy 只重啟 `ti.service`；autopilot 行程要吃到新碼需另
  `systemctl restart ti-autopilot`（或等任務邊界 execv 自我重載）。

## 四、雙審 escalate 時（升級給人）

`escalate` 的語意＝審查者無法在沙箱內核實證據（常見於治理關鍵 diff），交人裁決。
目前系統內**沒有**對 high-reversible 的人工放行參數（`human_approved` 只作用於
irreversible）——閉環設計由 #653 治理檢討裁決中。在那之前，operator 確認過
CI 綠＋內容合理後的後備手段是 OS 層手動部署：

```bash
cd /opt/ti && git pull --ff-only \
  && .venv/bin/pip install -e . -q \
  && .venv/bin/python -c "import studio.server" \
  && systemctl restart ti.service ti-autopilot \
  && sleep 5 && curl -s http://127.0.0.1:8021/api/health
```

- import 煙測失敗＝不要 restart（服務維持舊版）。
- 回滾：`git reset --hard <前一個健康 SHA>` 後重跑 pip＋restart。
- 這條路治理層零留痕——屬最後手段，事後應把緣由餵回治理檢討任務。

## 五、相關

- 治理規則與升階條件：`docs/guides/autonomy-governance.md`
- 監控與判卡死三驗證:`docs/guides/autopilot-monitoring.md`
- 端點盤點：`docs/loopback-endpoint-audit.md`
