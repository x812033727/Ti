# `GH_PAT` Token 輪替工作單（2026-07-10）

> 本工作單是 `docs/token-rotation-runbook.md` 三步驟主線的**可勾選執行清單**。
> 規格不另立新標準：四項 PAT 規格、驗證 DoD、殘留掃描指令一律以 runbook 為唯一權威。
> 本檔只負責照順序打勾、逐步標人工/AI、把驗證輸出貼回來留證。
>
> **鐵則：先發後撤，順序不可顛倒。** 撤舊必須留到最後；新 token 未通過輪替驗證 DoD 前，
> **絕不撤舊**。本工作單全文順序固定為
> **發新 → 更新 `.env` 與同名 repo secret 兩處 → 驗證 → 撤舊**，無任何撤舊提前路徑。
> 相容守門錨：發新 -> 更新 repo secret 與 `.env` -> 驗證 -> 撤舊；新 token 未驗證通過前，不得撤銷舊 token。
> 本文不記錄、不貼上 token 明文。
>
> **跨任務契約**：AI 可代勞段一律走 `scripts/verify_token_rotation.sh`；
> 守門測試為 `tests/docs/test_qa_token_rotation_script.py`。

## 執行順序總覽（鎖死）

```
[步驟 1] 發新 fine-grained PAT ──人工
   ↓
[步驟 2a] 更新兩處：本機 .env + 同名 repo secret GH_PAT ──人工
   ↓
[步驟 2b] 驗證新 token 生效 ──AI 可代勞
   ↓（驗證未通過 → 停在這裡，回步驟 2a 修正，絕不往下）
[步驟 3] 撤銷舊 token ──人工
   ↓
[收尾] 殘留 token 掃描 ──AI 可代勞
```

## 步驟 1（實際先做）：發新 fine-grained PAT — 【人工】

> AI 不得代行：產生 token 需登入 GitHub 帳號、UI 勾權限、複製一次性明文；此明文
> **不得**貼進任何 session 對話、commit 或工具輸出。

- [ ] **Token 類型：Fine-grained**（不要用 classic PAT）
- [ ] **Repository access：只選本 repo**（`Only select repositories`，不可選 `All repositories`）
- [ ] **Repository permissions：僅開 `Contents: Read and write`**（其餘保持 `No access`）
- [ ] **Secret 名稱固定為 `GH_PAT`**，且設定明確到期日
- [ ] 已複製新 token 一次性明文，準備寫入 `.env`／repo secret（**明文不進對話/版控/工具輸出**）

## 步驟 2a：更新兩處（`.env` + 同名 repo secret） — 【人工】

> 明文寫入動作屬人工；兩處都要更新，缺一會半死不活。

- [ ] 寫進本機 `.env` 的 `GH_PAT=...`（或部署環境對應設定）；`.env` 已在 `.gitignore`，明文不入版控
- [ ] 更新 repo secret：`Settings → Secrets and variables → Actions` 的同名 `GH_PAT`
- [ ] 確認沒有把 token 明文貼進對話、腳本輸出、commit、測試 fixture 或文件

## 步驟 2b：驗證新 token 生效 — 【AI 可代勞】

> 只讀驗證、不碰明文即可執行。**驗證通過前，絕不進入步驟 3。**

- [ ] 首選：綁定新 token 跑 `GH_TOKEN="$GH_PAT" gh auth status`，不可裸跑
- [ ] 無 `gh` CLI 時才退 `curl` 打 `/user`，回 `200` 即 token 本身有效
- [ ] `200` 只證身分、不證 scope；驗完後人工再核對步驟 1 四項規格確實套用

curl fallback 的 HTTP 200 只證明身分有效，不證 repository scope；scope 仍需人工依 runbook 四項規格核對。

### 貼證欄位：步驟 2b 驗證輸出

> 只貼 exit code／HTTP 狀態碼／帳號名等非敏感回報；絕不貼 token 明文。

```
（待人工在場、具備 $GH_PAT 時執行 `bash scripts/verify_token_rotation.sh --verify`，把輸出貼於此）
狀態：待人工（步驟 2b 需 $GH_PAT 在場，AI 無法代持明文）
```

## 步驟 3（實際最後做）：撤銷舊 token — 【人工】

> **唯有步驟 2b 驗證確認生效後**才做。刪 token 是帳號層級不可逆敏感操作，
> fine-grained PAT 無 API 可刪使用者自己的 token，只能走 UI，AI 不得代為撤銷。

- [ ] 確認步驟 2b 已通過（新 token 生效）
- [ ] 到 UI 撤舊：`Settings → Developer settings → Personal access tokens → Fine-grained tokens → 該舊 token → Delete`
- [ ] 撤舊後留意：若 CI／autopilot 仍在用舊值，會在發佈鏈以 403 失敗

## 收尾：殘留 token 掃描 — 【AI 可代勞】

撤舊完成後（或懷疑外洩時）掃兩處殘留明文：`history/*.jsonl`（session 事件逐行存檔，全欄掃）
與 session workspace（沙箱工作目錄，可能有 untracked 殘留）。

- [ ] 主指令：`gitleaks detect --no-git --source history/` 與 `--source <workspace-dir>`
- [ ] 零依賴 fallback：`grep -rnE 'gh[posur]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{20,}' history/ <workspace-dir>`
- [ ] 命中任何一筆都視為外洩，立即回 runbook 頂端重跑輪替

### 貼證欄位：`--scan` 掃描輸出

```
$ timeout 60 bash scripts/verify_token_rotation.sh --scan history/ .
[scan] skip 不存在目標: history/
[scan] gitleaks not found; using grep fallback
[scan] grep fallback 未發現殘留 token（掃描: .）
```

狀態：AI 可代勞段已執行；本工作區無 `history/` 目錄，已明確記錄 skip，workspace `.` 掃描未命中。
注意：步驟 1（發新）、步驟 2b（需 `$GH_PAT` 在場）與步驟 3（撤舊）仍待人工完成。

### 貼證欄位：`--report` 人工/AI 分界狀態表

```
$ timeout 60 bash scripts/verify_token_rotation.sh --report
== GH_PAT 輪替 人工/AI 分界狀態表 ==
規格唯一權威：docs/token-rotation-runbook.md（本表不內嵌四項 PAT 規格）

步驟 | 動作                                     | 誰做      | 狀態
-----+------------------------------------------+-----------+----------------------
1    | 產生新 fine-grained PAT（勾權限/複製明文） | 人工      | 待人工（GitHub UI）
2a   | 更新 .env + 同名 repo secret GH_PAT       | 人工      | 待人工（明文寫入）
2b   | 驗證新 token 生效（--verify）             | AI 可代勞 | 需 $GH_PAT 在場
3    | 到 UI Delete 舊 token                     | 人工      | 待人工（不可逆/無 API）
掃描 | 殘留 token 掃描（--scan）                 | AI 可代勞 | AI 執行

明示事項：
- 步驟 1（發新）與步驟 3（撤舊）待人工於 GitHub UI 完成，AI 不代行。
- 步驟 2b 首選 GH_TOKEN="$GH_PAT" gh auth status；若走 curl，回 200 只證身分有效、不證 scope；需人工回 runbook 核對四項規格才閉環。
- 先發後撤：新 token 未通過 --verify 前，絕不撤舊（會 403 斷鏈）。
```

狀態：AI 可代勞段已執行；報表明示步驟 1/3 待人工、curl 200 不證 scope。
本輪重跑結果：`--scan` 已完成且僅見 `history/` 缺失 skip；`--report` 已完成且明示步驟 1 與步驟 3 仍待人工於 GitHub UI。

## 人工 / AI 分界（總表）

| 步驟 | 動作 | 誰做 | 本輪狀態 |
|------|------|------|----------|
| 1 | 產生新 fine-grained PAT（勾權限、複製明文） | **人工** | 待人工（GitHub UI） |
| 2a | 更新 repo secret `GH_PAT` + 寫入 `.env` | **人工** | 待人工（明文寫入） |
| 2b | 跑 `GH_TOKEN="$GH_PAT" gh auth status` / `curl … /user` 驗證 | AI 可代勞 | 待人工在場給 `$GH_PAT` |
| 3 | 到 UI `Delete` 舊 token | **人工** | 待人工（不可逆、無 API） |
| 掃描 | 跑殘留 token 掃描（`--scan`） | AI 可代勞 | 已執行並回填 |
| 報表 | 跑 `--report` 分界狀態表 | AI 可代勞 | 已執行並回填 |

守門摘要：1. 發新 fine-grained PAT | 人工 | 待人工；3. 撤銷舊 token | 人工 | 待人工。

> **本輪明確界線**：步驟 1（發新）與步驟 3（撤舊）待**人工**於 GitHub UI 完成；
> 步驟 2b 需 `$GH_PAT` 在場才能實跑，AI 不代持明文。AI 本輪只代勞 `--scan`／`--report`
> 唯讀掃描與報表，並回填上方貼證欄位。

## 2026-07-20 生產洩漏面掃描補記

本輪依 `docs/token-rotation-runbook.md` 的「殘留 token 掃描」實跑生產工作目錄
`/opt/ti-autopilot-work`。

掃描範圍：

- `history/`：200 個 `*.jsonl`、200 個 `*.json`；以整個 `history/` 目錄掃描。
- `workspaces/`：159 個既有 session workspace；以整個 `workspaces/` 目錄掃描。
- 已下載 zip：0 個；repo 內未找到 `*.zip`。

實跑輸出：

```bash
$ timeout 60 bash scripts/verify_token_rotation.sh --scan history workspaces
[scan] gitleaks not found; using grep fallback
[scan] grep fallback 未發現殘留 token（掃描: history）
[scan] grep fallback 未發現殘留 token（掃描: workspaces）
```

```bash
$ timeout 60 find . -path './.git' -prune -o -path './.venv' -prune -o -path './node_modules' -prune -o -type f -name '*.zip' -print | wc -l
0
```

結論：

- 未發現 `ghp_` / `github_pat_` / `gho_` / `ghs_` / `ghr_` 形態的 GitHub token 殘留。
- 無命中，無需清理檔案。
- 本機未安裝 `gitleaks`，本輪依 runbook 使用 grep fallback；掃描輸出不含 token 明文。
