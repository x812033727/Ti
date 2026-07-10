# `GH_PAT` Token 輪替工作單（2026-07-10）

> 本工作單是 `docs/token-rotation-runbook.md` 三步驟主線的**可勾選執行清單**。
> 規格不另立新標準——四項 PAT 規格、驗證 DoD、殘留掃描指令一律以 runbook 為唯一權威，
> 本檔只負責「照順序打勾、逐步標人工/AI、把驗證輸出貼回來留證」。
>
> **鐵則（不可翻案）：先發後撤，順序不可顛倒。** 撤舊（步驟 3）名義上排第一，
> **實際執行時間軸必須留到最後**；新 token 未通過「輪替驗證 DoD」前，**絕不撤舊**（會 403 斷鏈）。
> 本工作單全文順序固定為 **發新 → 更新 `.env` 與同名 repo secret 兩處 → 驗證 → 撤舊**，
> 無任何先撤路徑。
>
> **跨任務契約（腳本依賴）**：AI 可代勞段（`--verify`/`--scan`/`--report`）一律走
> **`scripts/verify_token_rotation.sh`**（此路徑+檔名為跨任務契約，已落地）；其守門測試為
> `tests/docs/test_qa_token_rotation_script.py`。下方貼證欄位的輸出即由此腳本產生。

## 執行順序總覽（鎖死）

```
[步驟 1] 發新 fine-grained PAT ──人工
   ↓
[步驟 2a] 更新兩處：本機 .env  +  同名 repo secret GH_PAT ──人工
   ↓
[步驟 2b] 驗證新 token 生效（gh auth status 綁定 / curl 200） ──AI 可代勞
   ↓ （驗證未通過 → 停在這裡，回步驟 2a 修正，絕不往下）
[步驟 3] 撤銷舊 token ──人工
   ↓
[收尾] 殘留 token 掃描（history/ 與 workspace） ──AI 可代勞
```

---

## 步驟 1（實際先做）：發新 fine-grained PAT — 【人工】

> AI 不得代行：產生 token 需登入 GitHub 帳號、UI 勾權限、複製一次性明文；此明文
> **不得**貼進任何 session 對話、commit 或工具輸出。

到 GitHub `Settings → Developer settings → Personal access tokens → Fine-grained tokens →
Generate new token`，逐項核對 runbook 四項規格（**一項都不能放寬**）：

- [ ] **Token 類型：Fine-grained**（不要用 classic PAT）
- [ ] **Repository access：只選本 repo**（`Only select repositories`，**不可**選 `All repositories`）
- [ ] **Repository permissions：僅開 `Contents: Read and write`**（其餘保持 `No access`）
- [ ] **設定明確到期日（Expiration）**，並在到期前重跑本 runbook（不建議「無到期」）
- [ ] 已複製新 token 一次性明文，準備寫入 `.env`／repo secret（**明文不進對話/版控/工具輸出**）

## 步驟 2a：更新兩處（`.env` + 同名 repo secret） — 【人工】

> 明文寫入動作屬人工；兩處都要更新，缺一會半死不活。

- [ ] 寫進本機 `.env` 的 `GH_PAT=...`（或部署環境對應設定）；`.env` 已在 `.gitignore`，明文不入版控
- [ ] 更新 repo secret：`Settings → Secrets and variables → Actions` 的**同名 `GH_PAT`**
      （**不要改名、不要新建第二個**）

## 步驟 2b：驗證新 token 生效 — 【AI 可代勞】

> 只讀驗證、不碰明文即可執行。**驗證通過前，絕不進入步驟 3。**

- [ ] 首選：綁定新 token 跑 `gh auth status`（**不可裸跑**——裸跑只驗 keyring 舊 token，會假綠）
      ```bash
      GH_TOKEN="$GH_PAT" gh auth status
      ```
- [ ] 無 `gh` CLI 時才退 `curl` 打 `/user`，回 **`200`** 即 token 本身有效（`401`/`403` = 無效/被撤）
      ```bash
      curl -sS -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $GH_PAT" https://api.github.com/user
      ```
- [ ] ⚠️ **`200` 只證身分、不證 scope**：`/user` 不驗 `Contents: Read and write`；驗完 200 後
      **人工再核對步驟 1 四項規格**確實套用，才真正閉環

### 貼證欄位：步驟 2b 驗證輸出

> ⚠️ 只貼 exit code／HTTP 狀態碼／帳號名等**非敏感**回報；**絕不貼 token 明文**。

```
（待人工在場、具備 $GH_PAT 時執行 `bash scripts/verify_token_rotation.sh --verify`，把輸出貼於此）
狀態：⏳ 待人工（步驟 2b 需 $GH_PAT 在場，AI 無法代持明文）
```

## 步驟 3（實際最後做）：撤銷舊 token — 【人工】

> **唯有步驟 2b 驗證確認生效後**才做。刪 token 是帳號層級不可逆敏感操作，
> **fine-grained PAT 無 API 可刪使用者自己的 token**，只能走 UI，AI 不得代為撤銷。

- [ ] 確認步驟 2b 已通過（新 token 生效）
- [ ] 到 UI 撤舊：`Settings → Developer settings → Personal access tokens → Fine-grained tokens →
      該舊 token → Delete`
- [ ] 撤舊後留意：若 CI／autopilot 仍在用舊值，會在 `gh release create` 以 403 失敗
      （這正是「必須先確認新值生效」的原因）

---

## 收尾：殘留 token 掃描 — 【AI 可代勞】

撤舊完成後（或懷疑外洩時）掃兩處殘留明文：`history/*.jsonl`（session 事件逐行存檔，全欄掃）
與 session workspace（沙箱工作目錄，可能有 untracked 殘留）。指令以 runbook 為準：

- [ ] 主指令（成熟工具）：`gitleaks detect --no-git --source history/` 與 `--source <workspace-dir>`
- [ ] 零依賴 fallback（無 gitleaks，全前綴 `ghp_`/`github_pat_`/`gho_`/`ghs_`/`ghr_`）：
      `grep -rnE 'gh[posur]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{20,}' history/ <workspace-dir>`
- [ ] 命中任何一筆都視為外洩，立即回 runbook 頂端重跑輪替

### 貼證欄位：`--scan` 掃描輸出（AI 於任務 #4 回填）

實跑於 2026-07-10（無 gitleaks，走零依賴 grep fallback；全前綴 `ghp_`/`github_pat_`/`gho_`/`ghs_`/`ghr_`）：

```
$ bash scripts/verify_token_rotation.sh --scan history/
[scan] grep fallback 未發現殘留 token（掃描: history/）
# exit=0

$ bash scripts/verify_token_rotation.sh --scan workspaces/
[scan] grep fallback 未發現殘留 token（掃描: workspaces/）
# exit=0

$ bash scripts/verify_token_rotation.sh --scan history/ workspaces/
[scan] grep fallback 未發現殘留 token（掃描: history/ workspaces/）
# exit=0
```

狀態：✅ `--scan` 已回填。真實 `history/`（session 事件存檔）與 `workspaces/`（沙箱工作目錄）兩處實跑，
**未發現殘留 token**（exit 0）。掃描器僅報「檔名+筆數」並遮蔽命中內容，本身零明文外洩
（判別力另由黑/白樣本於守門測試 `test_qa_token_rotation_script.py` 實跑驗證：黑樣本 exit 2、白樣本 exit 0）。
注意：本欄雖已回填，整體輪替的步驟 1（發新）、步驟 3（撤舊）與步驟 2b（`--verify`）仍**待**人工完成，見下方分界表。

### 貼證欄位：`--report` 人工/AI 分界狀態表（AI 於任務 #4 回填）

實跑於 2026-07-10（`exit=0`）：

```
$ bash scripts/verify_token_rotation.sh --report
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
- 步驟 2b 若走 curl，回 200 只證身分有效、不證 scope；需人工回 runbook 核對四項規格才閉環。
- 先發後撤：新 token 未通過 --verify 前，絕不撤舊（會 403 斷鏈）。
```

狀態：✅ 已回填。`--report` 恆 exit 0，人工/AI 分界表明示：步驟 1/3 待人工、curl 200 不證 scope 需人工核對四項規格。

---

## 人工 / AI 分界（總表）

| 步驟 | 動作 | 誰做 | 本輪狀態 |
|------|------|------|----------|
| 1 | 產生新 fine-grained PAT（勾權限、複製明文） | **人工** | ⏳ 待人工（GitHub UI） |
| 2a | 更新 repo secret `GH_PAT` + 寫入 `.env` | **人工** | ⏳ 待人工（明文寫入） |
| 2b | 跑 `GH_TOKEN="$GH_PAT" gh auth status` / `curl … /user` 驗證 | AI 可代勞 | ⏳ 待人工在場給 `$GH_PAT` |
| 3 | 到 UI `Delete` 舊 token | **人工** | ⏳ 待人工（不可逆、無 API） |
| 掃描 | 跑殘留 token 掃描（`--scan`） | AI 可代勞 | ✅ 本輪任務 #4 執行並回填 |
| 報表 | 跑 `--report` 分界狀態表 | AI 可代勞 | ✅ 本輪任務 #4 執行並回填 |

> **本輪明確界線**：步驟 1（發新）與步驟 3（撤舊）待**人工**於 GitHub UI 完成；
> 步驟 2b 需 `$GH_PAT` 在場才能實跑，AI 不代持明文。AI 本輪只代勞 `--scan`／`--report`
> 唯讀掃描與報表，並回填上方貼證欄位。
