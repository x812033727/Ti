# 自治治理與第 3／4 階升階

Ti Studio 的自治控制面預設不會替既有核心流程偷偷開啟。新專案會建立
`autonomy-policy.v1.json` 並從 `shadow` 起步；核心 `ti-studio` 必須由管理者建立／更新政策後，
才會進入新版治理流程。

## 運行模式與風險

- `shadow`：執行本機推演與客觀閘門，但禁止 push、PR、merge、deploy。
- `canary`：只允許可逆工作；政策或基線不明時 fail-closed。
- `full`：允許通過所有治理閘門的工作。
- `degraded`：只允許低風險工作。
- `paused`：不接新任務。

風險為 `low`、`medium`、`high-reversible`、`irreversible`。未分類風險會升級成人工裁決；
不可逆操作須有管理者人工核可。`high-reversible` 在 merge／deploy 前還必須具備 dry-run、備份、
影響範圍上限、已驗證 rollback，以及兩個不同 provider 對同一 `diff_sha`／`evidence_sha`
產生的明確 `approve` verdict 與理由。Autopilot 會把完全相同、具 SHA256 的 diff 與證據送給兩個
不同 provider 獨立審查；provider 不可用、回傳格式錯誤、hash 不符、同 provider 或 verdict 衝突
都會升級而不執行。覆蓋率以實際 merge／deploy 決策逐關計算；同一 run 的 merge 核可不能冒充
後續 deploy 核可。AI／discovery 的批次任務入口會無條件丟棄 `human_approved`，不可逆操作只能
由 admin 保護的單筆人工入口批准，並記為 `product_decision` 介入。

Stage 3+ 的一般 low／medium 任務在 deploy 關卡會升為 `high-reversible`，但既有
`high-reversible`／`irreversible` 風險永不降級。一般部署的 rollback evidence 由最近 28 日內
無失敗的 verified rehearsal、當前健康 revision 與有界回復機制組成，不採信任務自行宣告的
布林值；任務本身原已屬高風險時仍必須提供該操作專屬的 rollback 證據。

Stage 4 政策必須同時宣告非空的 `intent.north_star`、`success_metrics` 與
`forbidden_actions`。核心自評、專案改良和每日 intent discovery 在 Stage 4 都只使用同一份
結構化規畫證據：版本化 intent、7 日實際指標、30 日事故，以及 eligible 且仍有效的 backlog；
舊的環境 north-star 不會覆蓋這份政策。

每個 Stage 4 政策也版本化保存 `limits.closed_loop_slo_min` 與 `slo_min_eligible`。排程器以
7 日逐專案 eligible 樣本計算從意圖到健康部署的閉環率；樣本達門檻且低於 SLO 時，五分鐘內
把該專案降為 `degraded`（已 `paused` 則維持暫停），寫入可配對的 violation/control 事件並推播。

外部專案在進入 Stage 3 canary 前就必須設定 `deployment.health_url`、`healthy_field` 與
`revision_field`，讓每次開工能核對 deployed revision 與來源 SHA；Stage 4 再沿用同一契約
證明合併後的新 revision 已健康。探針只允許公開 HTTPS/443，不接受 query/憑證、不追蹤
redirect，並將 DNS 釘到已驗證的
公開 IP；回應必須是有界 JSON，健康欄位為 true/`ok`/`healthy`，且 revision 必須
精確等於這次 GitHub merge SHA。因此「舊版本仍健康」或「PR 只是合併但尚未部署」
都不會被計為 `healthy_deployed`。核心 `ti-studio` 沿用獨立的
`deploy.redeploy` health＋blackbox＋rollback 契約。範例：

```json
{
  "deployment": {
    "health_url": "https://product.example/healthz",
    "healthy_field": "ok",
    "revision_field": "build.git_sha",
    "timeout_s": 300,
    "poll_interval_s": 10
  }
}
```

外部專案若合併後健康探針失敗，會執行真正的 rollback，而不只採信任務裡的布林宣告：遠端
base 必須仍精確指向這次壞 merge、先前健康 SHA 必須是其祖先，revert 後 tree 必須精確等於
先前健康版本；全部成立才建立 rollback PR，並再次等待 CI、合併與 health/revision 驗證。
base 已被別人推進、revert 衝突或內容不等時一律不 push，改為專案煞車與外部告警。

政策的 `source` 應釘住 `repo`、`workspace`、`publish_repo` 與 `lane`。每次任務開工還會核對
deployed SHA、source SHA、base branch、來源工作樹潔淨度與部署身分證據；本機部署另核對部署
工作樹潔淨度。canary／full／degraded 下任何漂移或未證明狀態都會觸發持久煞車。
觀察窗 preflight 預設直接檢查 `TI_AUTOPILOT_WORK_DIR` 的實際來源 clone，並與部署目錄分開
驗證；因此部署本身乾淨但仍有 in-flight／遺留來源修改時，也不得開始觀察窗。
任務的 `eligible` 必須在開工前明定為布林值；舊資料的 `unknown` 在 shadow 只留下 warning，
進入 canary／full／degraded 後則直接拒絕，不能藉缺欄縮小分母。

## API

- `GET /api/autonomy/status`：平台與各專案 stage、mode、煞車、預算、指標與近期轉換。
- `GET /api/autonomy/events`：v1 事件；預設同時投影舊 audit/history，缺欄明示 `unknown`。
- `GET /api/autonomy/preflight`：只讀檢查來源 SHA、任務收斂、政策 source、shadow rollout、
  外部通知、全部必要紅色演練、rollback、Stage 4 部署健康契約與報告鏈，且不回傳秘密或
  workspace 絕對路徑。
- `POST /api/autonomy/preflight/snapshot`：管理員保存帶內容 hash 的 preflight 快照；即使紅燈
  也會保存，避免只挑綠燈證據。
- `POST /api/autonomy/rollback-drills`：任務全數收斂後，在每個專案的隔離 worktree 對目前
  HEAD 做本機 revert，只有結果 tree 精確等於第一 parent 才寫 verified success；不 push、
  不開 PR、不部署，也不接受人工手寫的 success 取代證據。
- `PUT /api/autonomy/platform-mode`：管理員同步把核心與所有現有專案切到 `shadow`、`canary`
  或 `full`；`canary` 要求所有政策目標至少 Stage 3，`full` 只允許已正式達成 Stage 4 的平台；
  管理 API 在進入 `canary` 前還會重跑 Stage 3 preflight。寫到一半失敗會回復已變更政策並
  觸發全域煞車。
- `POST /api/autonomy/promote`：成熟度全綠後，以內容定址快照正式把全平台升到 Stage 3 或 4；
  不允許跳階，Stage 4 的觀察窗只從正式 Stage 3 升階事件之後起算。
- `GET|PUT /api/autonomy/policies/{project_id}`：讀取／管理員原子更新政策。
- `POST /api/autonomy/brakes/{global|project}/clear`：管理員解除煞車並留下營運救援介入。
- `POST /api/notify/red-drills`：安全送出全部必要的合成紅色告警；不執行真實 rollback 或部署。

`/api/metrics` 與 `/api/autopilot/audit-trend` 的既有欄位不變，新增 `autonomy` 區塊。任務 API
可選填 `risk`、`eligible`、`exclusion_reason`、`rollback`、`diff_sha`、`evidence_sha`、
`approval_verdicts` 與 `human_approved`。排除任務必須在建立時提供原因。

## 升階規則

每日報告寫入 `autonomy/maturity-reports/YYYY-MM-DD.json`，包含計算版本、內容 hash 與前一份
報告 hash。缺日、報告被修改或任一日紅燈都會中斷連續觀察窗；原始 v1 audit 可重新計算結果。

納管核心政策後，排程器每小時冪等檢查 `autonomy/weekly-improvements/YYYY-Www.json`。每週只會
依完成率、最高頻失敗 outcome、介入、告警、rollback 與成本弱項產生最多三項具驗收標準的工作；
會跨所有專案 backlog 與最近 12 份週報做相似去重，拒絕純盤點／純證據文件。週報亦含內容 hash，
遭修改時停止重用並留下可觀測錯誤。

第 3 階必須連續 28 天，且至少 20 個 eligible 任務，同時達到 90% zero-touch、80% 完成率、
每週最多兩次且僅為補背景介入、全部必要紅色告警五分鐘內送達、每個現有專案 rollback 100%、audit／成本證據完整、
零重大事故與每日平台成本不超過 US$100。

第 4 階會在所有納管專案以不可變快照正式達成第 3 階後另起 28 天觀察窗。政策 `stage` 是要啟用
並驗證的目標能力，狀態 API 的 `stage` 才是正式達成階段，兩者不會互相冒充。每個專案至少 10 個 eligible 任務，
從意圖／異常到健康部署的 zero-touch 閉環率須達 85%；高風險操作、例外通知、audit、SLO 煞車、
rollback 與成本硬上限也必須全數通過。狀態 API 只回報 readiness，不會自行篡改正式 stage。
升階當天只有完整落在正式 Stage 3 升階時間之後的 UTC 日才會計入 Stage 4 視窗。

## 操作順序

1. 設定外部 webhook 或 Telegram，更新各專案 intent、source、deployment 健康契約與限制。
2. 在 `shadow` 跑過基線與決策黑樣本，確認沒有外部寫入。
3. 執行紅色通知與 rollback 演練；確認 delivery evidence 的 latency 是從事件產生起算，且
   `rollback_result` 存在。
4. 以管理員政策更新進入 `canary`，持續查看 `/api/autonomy/status` 的 blocking reasons。
5. 以平台模式 API 讓所有現有專案同步進入 canary；單一專案可局部降到 degraded/paused，
   未授權的模式漂移或平台 rollout 寫入失敗則全平台停接新任務。
6. readiness 全綠後呼叫正式升階 API；它會保存不可變 promotion snapshot，再寫入單一平台升階事件。

任何成本不明、audit 寫入失敗、來源漂移、同 provider 雙審、證據不一致或 rollback 缺失都不會
被當作成功或零介入；控制面會拒絕外部動作，待管理者排除原因並顯式解除煞車。
