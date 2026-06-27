# 動態流程（Dynamic Workflow）

把工作室原本寫死在 `StudioSession._run()`／`_work_task()` 的固定骨架，改成「一份宣告式
stage 序列」驅動。讓全流程（架構討論→任務波次→整合→Demo→發佈）都能依需求裁剪、換參與者、
插入「動態 step」（PM 運行時決定下一步找誰）。

## 核心概念

- **單一真相**：`studio/workflow.py` 的 `default_workflow()` 是「等價於現有寫死骨架」的內建
  定義（不存檔）。未選任何 workflow（WS 握手不帶 `workflow`，或 `StudioSession(workflow=None)`）
  ＝載入它 → 走同一段直譯器、同一順序 → 與重構前位元級等價。autopilot／improver 行為不變。
- **直譯器**：`_run_workflow()` 按 `stages` 順序派發 `_stage_<type>` handler；中間產物寫在
  session 黑板（`self._clarify_note`／`_pm_plan`／`_design_note`／`_all_ok`／…）。
- **檔案驅動**：客製流程存單檔 `<TI_ROLES_DIR>/workflows.yaml`（頂層 `workflows:` 列表），
  與 `groups.yaml` 同範式（temp＋rename 原子寫、寫入時硬驗證、熱讀）。

## 來源與選用

| 來源 | 怎麼用 |
|---|---|
| 內建預設 | 不指定即走，名稱為「預設流程」，永遠可選、不可被同名檔案覆蓋 |
| `workflows.yaml` | 經 `GET/POST/PUT/DELETE /api/workflows` 或直接編檔；寫入走 `require_admin` |
| 啟動選用 | 前端啟動列「動態流程」下拉，或 WS 握手帶 `{"workflow": "<名稱>"}` |

## Schema

兩層：**session 級 pipeline**（巨觀骨架）＋ **task 級 pipeline**（單任務內，內嵌於 `build`）。

Session 級 stage 型別（`STAGE_TYPES`）：
`clarify`／`research`／`decompose`／`discuss`／`build`／`integrate`／`demo`／`wrap_up`／
`publish`／`dynamic`。

Task 級 stage 型別（`TASK_STAGE_TYPES`，內嵌於 `build.task_pipeline`）：
`implement`／`review`／`gate`／`dynamic`。

Stage 欄位（pydantic `extra="forbid"`，未知欄位報錯）：

| 欄位 | 說明 |
|---|---|
| `type` | 必填，對應層級的合法型別 |
| `name` | 選填，事件 phase 顯示名（預設用型別） |
| `roles` | 選填 `list[role_key]`（discuss／dynamic 的參與者；缺省＝沿用內建選角） |
| `assignee` | 選填單一 role_key（implement 等單人 stage） |
| `mode` | 選填 `round_robin`｜`parallel`｜`single` |
| `gate` | 選填 `list[{role, verdict, optional}]`，verdict ∈ 白名單 |
| `max_rounds` | 選填 int≥0（0＝取對應 config 旋鈕） |
| `optional` | bool，預設 False |
| `when` | 選填條件 token：`has:<role_key>`（角色在場）／`flag:<CONFIG_NAME>` |
| `budget` | dynamic 專用，最大 hop 數 int≥0（0＝取 `config.DYNAMIC_STEP_BUDGET`） |
| `fallback` | dynamic 專用，PM 給不出合法下一步時的退路 role（預設 engineer） |
| `task_pipeline` | 僅 `build` 可有（且必填）：task 級 stage 列表 |

**verdict 白名單**（只能引用 `flow.py` 既有判定，不得注入程式碼）：
`qa_passed`／`senior_approved`／`security_approved`／`critic_blocks`／`pm_done`。

> 客觀閘門（自測 exit code 硬否決）、停滯守門（`is_stalled`）、軟性收尾（`_should_wind_down`）
> 等引擎不變式刻意**不可**被 workflow 配置掉（反 reward-hacking）。

### task_pipeline 生效範圍

`build.task_pipeline` 由 `_work_task` 完整讀取（**預設定義逐字重現今日行為**）：

- **implement.assignee**：實作者角色（預設 `engineer`）。不在場時退回 engineer。
- **review.gate**：reviewer 集合——有序 `(role, verdict)`，並行發言、過濾在場。預設
  `qa/senior/security`；可增刪、換人，**含非核心角色**（如把 `architect` 當 reviewer，
  其 verdict 取自白名單）。已知角色（qa/senior/security）用專屬 prompt，其餘依 verdict 自動組
  generic prompt。security 不在場自動濾掉（重現今日）。
- **review.max_rounds**：>0 時覆寫單任務輪數上限（預設取 `config.TASK_MAX_ROUNDS`）。
- **gate（critic）**：含 `gate` stage（verdict＝`critic_blocks`）才啟用放行前異議關卡
  （仍受 `TI_CRITIC` 控制）。省略 → 跳過 critic。
- **dynamic（任務內動態追加把關）**：含 `dynamic` stage 時，標準審查＋critic 通過後，PM 有界地
  動態挑成員追加把關（`budget` 上限、`fallback` 退路）。被追加成員以 `異議: 成立/不成立` 判定；
  任一成立 → 退回再修。無 dynamic stage → 直接放行（零行為變更）。防呆同 session 級 dynamic
  （`_stop`/`is_stalled`/`validate_assignees` fallback）。

> 不被 task_pipeline 影響的硬性護欄：客觀閘門（自測 exit code）、交付前自測、停滯守門、
> reflexion、critic 收斂預算（`TI_CRITIC_MAX_REJECTS`）——這些是引擎不變式，照常運作。

## 範例

### 等價內建預設骨架（`default_workflow()`）
```yaml
name: 預設流程
stages:
  - {type: clarify}
  - {type: research, optional: true, when: has:researcher}
  - {type: decompose}
  - {type: discuss}            # 不硬指定 roles，沿用 group/architect 既有選角
  - type: build
    task_pipeline:
      - {type: implement, assignee: engineer}
      - type: review
        mode: parallel
        gate:
          - {role: qa, verdict: qa_passed}
          - {role: senior, verdict: senior_approved}
          - {role: security, verdict: security_approved, optional: true}
      - {type: gate, roles: [pm], gate: [{role: pm, verdict: critic_blocks}]}
  - {type: integrate, optional: true, when: has:devops}
  - {type: demo}
  - {type: wrap_up}
  - {type: publish}
```

### 精簡客製（跳過調研/資安，PM 動態決定要不要找高工）
```yaml
name: 快速原型
stages:
  - {type: decompose}
  - type: build
    task_pipeline:
      - {type: implement, assignee: engineer}
      - type: review
        gate: [{role: qa, verdict: qa_passed}]
  - {type: dynamic, budget: 2, fallback: engineer}
  - {type: demo}
  - {type: wrap_up}
```

## 動態 step（`dynamic`）

`_stage_dynamic` 是有界迴圈：每 hop 餵 PM 黑板摘要＋roster（含角色描述）＋需求，要求輸出
`下一步: <role_key>` ＋ `指示: <做什麼>`，或 `下一步: 結束`。防呆全部沿用既有範式：
`budget` 硬上限 hop 數、每圈先檢查 `_stop`／`_should_wind_down`、非法角色經
`flow.validate_assignees` fallback、PM 連續高相似決策由 `flow.is_stalled` 收斂、每次發言走
`_speak`（號誌節流＋provider-unavailable 穿透，不誤判「未達完成」）。

## 相關設定

| env | 預設 | 說明 |
|---|---|---|
| `TI_ROLES_DIR` | `<repo>/roles` | `workflows.yaml` 落點（與角色/小組同目錄） |
| `TI_DYNAMIC_STEP_BUDGET` | `3` | dynamic stage 未指定 `budget` 時的 hop 上限（空字串容錯） |
