# `GH_PAT` Token 輪替 Runbook

> 本文件是 `GH_PAT`（發佈鏈用的 fine-grained Personal Access Token）**輪替主線操作手冊**。
> 目的：把「先做什麼、後做什麼、哪一步是人工、哪一步 AI 能代勞」寫死，避免輪替時把
> `.env` 或 CI 弄成半死不活（新的還沒生效、舊的先被撤，`gh release create` 直接 403 斷鏈）。
>
> 本檔與 `CLAUDE.md` 的「發佈鏈 DoD 與 `GH_PAT` 設定」同源：規格不另立新標準，只補**輪替步驟**
> 與**殘留掃描／驗證 DoD**。改動本檔前，先確認沒有和 `CLAUDE.md` 的 `GH_PAT` 四項規格牴觸。

## 最重要的一條規則：先發後撤，順序不可顛倒

**執行順序固定為：發新 → 更新 `.env` 並驗證可用 → 撤銷舊。**

概念上有三個動作——**撤銷舊 token、發新 fine-grained PAT、更新 `.env`**——但**實際執行的時間軸**
必須是「**先發新、驗證可用、再撤舊**」。也就是說：撤銷舊 token 這個動作雖然名義上排在清單第一，
**實際上要留到最後一步做**。

**先撤後發是錯的、會斷鏈**：一旦先把舊 `GH_PAT` 撤掉，在新 token 寫入 repo secret／`.env`
並驗證生效之前，任何 `gh release create` 或 autopilot 發佈都會以 **403** 失敗。所以：

> ⚠️ **先發後撤，順序不可顛倒。** 新 token 未經驗證（見「輪替驗證 DoD」章節）**不得**撤舊。

## 三步驟主線

### 步驟 1（實際先做）：發新 fine-grained PAT — 人工

到 GitHub `Settings → Developer settings → Personal access tokens → Fine-grained tokens →
Generate new token`，**沿用 `CLAUDE.md` 既定的 `GH_PAT` 四項規格，一項都不能放寬**：

1. **Token 類型：Fine-grained**（不要用 classic PAT）。
2. **Repository access：只選本 repo**（`Only select repositories`，**不可**選 `All repositories`）。
3. **Repository permissions：僅開 `Contents: Read and write`**（其餘保持 `No access`）。
4. **Secret 名稱固定為 `GH_PAT`**：新 token 產生後，到 repo `Settings → Secrets and variables →
   Actions` **更新同一個名為 `GH_PAT` 的 secret**（不要改名、不要新建第二個）。

另外**務必設定到期日（Expiration）**：fine-grained PAT 到期會自動失效，等同自動撤銷，可降低長期
外洩風險。個人專案雖可設「無到期」，但**不建議**；請設一個明確到期日並在到期前重跑本 runbook。

> **為什麼是人工**：產生 token 需登入 GitHub 帳號、在 UI 勾選權限並複製一次性明文；此明文
> **不得**貼進任何 session 對話、commit 或工具輸出。AI **不得**代為產生或持有此明文。

### 步驟 2：更新 `.env` 並驗證 — 人工落 secret／AI 可協助驗證

1. 把新 token 寫進本機 `.env` 的 `GH_PAT=...`（或部署環境的對應設定），**人工操作**，明文不入版控
   （`.env` 已在 `.gitignore`）。
2. 同步更新 repo secret `GH_PAT`（見步驟 1 第 4 點）。
3. **立即驗證新 token 可用**（見「輪替驗證 DoD」章節的 `gh auth status` / `curl … /user` 回 200
   判定）。**驗證通過前，絕不進入步驟 3。** 這步的「跑指令驗證」AI 可代勞，但寫入 `.env`／secret
   的明文動作仍是人工。

### 步驟 3（實際最後做）：撤銷舊 token — 人工

**唯有步驟 2 驗證新 token 確認生效後**，才撤銷舊 token。

- **fine-grained PAT 無 API 可刪使用者自己的 token**，必須走 UI：
  `Settings → Developer settings → Personal access tokens → Fine-grained tokens →
  該舊 token → Delete`。
- 撤銷後，舊 token 立即失效；若此時 CI／autopilot 仍在用舊值，會在 `gh release create` 以 403
  失敗——這正是「必須先確認新值已生效」的原因。

> **為什麼是人工**：刪除 token 是帳號層級的不可逆敏感操作，且無 API 入口，只能在 GitHub UI
> 手動 `Delete`。AI **不得**代為撤銷。

## 人工 / AI 分界（總表）

| 步驟 | 動作 | 誰做 | 說明 |
|------|------|------|------|
| 1 | 產生新 fine-grained PAT（勾權限、複製明文） | **人工** | 需登入帳號＋一次性明文，AI 不得持有 |
| 1/2 | 更新 repo secret `GH_PAT`、寫入 `.env` | **人工** | 明文寫入，不入版控、不進對話 |
| 2 | 跑 `gh auth status` / `curl … /user` 驗證新 token 生效 | AI 可代勞 | 只讀驗證，不碰明文即可執行 |
| 3 | 到 UI `Delete` 舊 token | **人工** | 不可逆、無 API，只能 UI 手動 |
| 掃描 | 跑殘留 token 掃描指令 | AI 可代勞 | 見「殘留 token 掃描」章節 |

## 延伸章節

- **殘留 token 掃描**：輪替後如何掃 `history/*.jsonl` 與 session workspace 的殘留明文
  （`gitleaks` 主指令 ＋ `grep -rnE` 零依賴 fallback，涵蓋 `ghp_/github_pat_/gho_/ghs_/ghr_`
  全前綴）。
- **輪替驗證 DoD**：`gh auth status` 或 `curl -H "Authorization: Bearer $GH_PAT"
  https://api.github.com/user` 回 200 才算生效；到期／撤銷會在 `gh release create` 以 403 斷鏈。
