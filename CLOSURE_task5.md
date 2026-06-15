# 任務 #5 關閉說明 — GH_PAT secret 前置文件化

純文件化任務：把「`GH_PAT` repo secret 為端對端驗收前置」明文寫入 PR 描述，並釘死
「**未設 secret 時 `Create release`（step 5）以 403 失敗屬預期行為、非 bug**」這一判定，
避免未來 reviewer／驗收者把預期的權限失敗誤報為缺陷。

本任務**零生產碼異動**——既有實作（publish-release.yml step 1 PAT guard + step 5 Create release）
已正確；本檔僅補足規格產物（PR 描述）這一交付物。

---

## ⬇️ 以下整段為 PR 描述（請於開 PR 時貼入 PR body）

### 端對端 AC 前置：`GH_PAT` repo secret（必讀）

本 PR 的發佈鏈 `publish-release.yml` → `release-smoke.yml` 之**端對端觸發**依賴一個
**repo secret `GH_PAT`**。此 secret **無法在 CI 沙箱／本工作目錄內驗證**，必須由具 repo
admin 權限者手動建立。

> ⚠️ **未設 `GH_PAT` 時，`Create release`（step 5）會以 HTTP 403 失敗——這是預期行為，不是 bug。**
> 請勿將此 403 當作回歸缺陷處理。根因與處置如下。

#### 為什麼一定要 PAT（觸發死結）

GitHub 防遞迴機制：以內建 `GITHUB_TOKEN` 建立的 release **不會**觸發下游 workflow，
`release-smoke.yml` 的 `on: release: types: [published]` 將永不啟動，整條鏈形同未啟用。
解法是以 **PAT 身分**建立 release——PAT 產生的 `release:published` 事件才能正常傳播到下游。
因此 step 5 的 `GH_TOKEN` 綁的是 `secrets.GH_PAT`（非 `GITHUB_TOKEN`）。

#### step 1 guard 與 step 5 403 的分工（兩種未設情境）

| 情境 | 攔截點 | 訊號 |
|---|---|---|
| `GH_PAT` **完全未設**（空字串） | step 1 `Verify PAT`（`test -n "$GH_TOKEN"`） | step 1 即 fail，error log 直指「忘了設 secret」 |
| `GH_PAT` **已設但過期／被撤銷／scope 不足** | step 5 `Create release` | gh CLI 回 **403**，屬預期權限失敗 |

step 1 只能偵測「空字串」，無法驗 PAT 是否有效；過期/無權的 PAT 會通過 guard，
到 step 5 才以 403 失敗。**兩種情境的失敗都非 bug，而是「前置 secret 未正確配置」的預期回饋。**

#### 如何設定（解除前置）

1. 建立 **Fine-grained PAT**，scope 僅授予本 repo 的 **`Contents: Read and write`**
   （建立 release 所需的唯一 scope；classic PAT 則僅勾 `repo`）。
   切勿授予 `admin:org`／`repo:all` 等過大 scope——PAT 洩漏的爆炸半徑與 scope 等大。
2. 至 repo **Settings → Secrets and variables → Actions → New repository secret**，
   名稱填 `GH_PAT`，值貼上述 token。
3. 設妥後，push 一次 `v*` tag 即可觀察 `publish-release` 建立 release →
   `release-smoke` 被 `release:published` 觸發一輪，端對端鏈閉合。

#### 本 PR 在 CI／本地已可驗的範圍（AC#1–#4、#6）

| AC | 內容 | 可驗性 |
|---|---|---|
| #1 | `publish_release.py` 由 CHANGELOG 渲染非空 body.md，含頂層 Breaking Changes 區塊 | ✅ 本地實跑 |
| #2 | step 5 為 `gh release create "$TAG" -F body.md`，`GH_TOKEN=secrets.GH_PAT`，tag 經 env 傳入、run 內無 `${{ }}` 展開 | ✅ 守護測試 |
| #3 | PAT 建立的 `release:published` 串起 release-smoke；guard 攔 secret 未設 | ✅ 結構/邏輯斷言（觸發本身屬 #5 前置） |
| #4 | 守護測試雙層綠、mutation 翻紅 | ✅ 本地實跑 |
| #6 | 四支測試 0 失敗 | ✅ 本地實跑 |
| #5 | **端對端真觸發** | ⚠️ **需先設 `GH_PAT` secret，非沙箱內可驗——即本前置** |

---

## 驗收實跑（本地，全綠）

| 指令 | 結果 |
|---|---|
| `python3 scripts/publish_release.py` | 渲染出非空 `body.md`，含頂層 `## ⚠️ Breaking Changes` |
| `pytest …task2/task3/task4/dry_run -q` | **68 passed** |

## 判定

`決議: 完成` — PR 描述已明文標注 `GH_PAT` 前置；釘死「step 5 403 為預期、非 bug」並區分
step 1（空字串）與 step 5（過期/無權）兩種失敗情境；對應 AC#5。零生產碼異動，本地測試全綠。

**執行指令: `python3 scripts/publish_release.py && python3 -m pytest tests/autopilot/test_qa_task2_release_body.py tests/autopilot/test_qa_task3_release_trigger_chain.py tests/autopilot/test_qa_task4_publish_workflow_guard.py tests/autopilot/test_release_pipeline_dry_run.py -q`**
