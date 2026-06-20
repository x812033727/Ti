# 專案協作記憶

## 架構鐵則：專案 repo vs Ti 主核心 repo（雙軌路由）

- **專案改動**進專案 repo（`projects.effective_repo`：per-project `publish_repo` → 全域 `TI_PUBLISH_REPO`）。
- **Ti 核心框架改動**（orchestrator／runner／發佈流程等）一律路由到 **`config.CORE_REPO`（固定
  `x812033727/Ti`）的獨立 PR**，**絕不混入專案 repo**。
- 判定方式：由專家在討論／檢討中以結構化行 `核心改動: <描述>` 表態（`flow.parse_core_changes`），
  消費端以 `backlog.add_items(core, source="core")`（省略 `state_dir`＝核心 backlog）路由，
  autopilot 在核心 repo 實作並開 PR。詳見 `ARCHITECTURE.md`「專案 repo 與 Ti 主核心 repo」。

## 發佈鏈 DoD 與 `GH_PAT` 設定

- 發佈鏈契約：`.github/workflows/publish-release.yml` 只在 `push.tags: v*` 建立 GitHub release；
  `.github/workflows/release-smoke.yml` 只用 `release: published` 接下游 smoke。建立 release 的
  `GH_TOKEN` 必須維持 `secrets.GH_PAT`，不可換回 `GITHUB_TOKEN`，否則 GitHub 防遞迴機制會讓
  `release-smoke` 不被觸發。
- `GH_PAT` 設定指引：建立 Fine-grained PAT；Repository access 務必只選本 repo（非 all-repos）；
  Repository permissions 僅開 `Contents: Read and write`；
  到 repo `Settings -> Secrets and variables -> Actions` 建立 secret，名稱固定為 `GH_PAT`。
- `GH_PAT` 到期或被撤銷時，Step 5 `gh release create` 會以 403 失敗；輪替後只更新同一個 repo
  secret `GH_PAT`，不要改 workflow token 路由。
- 發佈 DoD：`body.md` 必須由 `scripts/publish_release.py` 產生，版本來自
  `studio.release_note.pyproject_version()`，Breaking heading 來自同一 Python SSOT，不在 YAML 硬寫；
  發佈前需重跑 release 相關守護測試與 `python3 scripts/publish_release.py`。
- 驗證邊界必須明講：單元/守護測試為半閉環，真實 `v*` tag-push 端到端尚待生產驗證。換句話說：
  真實 tag-push 端到端尚待生產驗證；第一次正式打 `v*` tag 後，需確認
  `publish-release -> release-smoke` 生產鏈實際通過。
- 本輪不加 `--verify-tag`：現有觸發條件已由 `push.tags: v*` 保證 tag 存在，且 workflow 另有
  `github.ref_name == v{pyproject_version()}` fail-fast。若未來新增 `workflow_dispatch` 手動發佈，需重審此決策。

## 工程師 — 長期經驗

### Release 發佈鏈操作記憶

`publish-release.yml` 建立 GitHub Release 時固定使用 repo secret `GH_PAT`，不要改回 `GITHUB_TOKEN`；用
`GITHUB_TOKEN` 建 release 不會觸發下游 `release-smoke.yml` 的 `release: published` workflow。

`GH_PAT` 設定規格固定如下：使用 fine-grained PAT，只授權本 repo，不要選 all repositories；Repository
permissions 設 `Contents: read and write`；secret 名稱固定為 `GH_PAT`。若 token 過期或被撤銷，`Verify
PAT` 只能檢查非空，實際會在 Step 5 `gh release create` 以 403 失敗；輪替時到 repo Settings →
Secrets and variables → Actions 更新同名 `GH_PAT`。

真實 `v*` tag-push 端到端尚待生產驗證，單元/守護測試為半閉環；目前只能證明
`push tag -> render body -> gh release create 設定 -> release: published smoke 設定` 的結構正確，不代表
GitHub 生產環境 E2E 已實跑過。

本輪不補 `--verify-tag`：現行 workflow 只由 `on.push.tags: v*` 觸發，tag 已存在，且 `Assert tag matches
version` 會比對 `github.ref_name` 與 `v{pyproject_version()}` fail-fast；在未加入 `workflow_dispatch` 手動發佈前，
`--verify-tag` 不需作為驗收必要硬化，避免為重複保護增加範圍。

### 非預期輸出：先懷疑自己的命令，絕不先怪「環境污染」
**慘痛教訓（CI 修復任務）**：我把自己命令的真實後果——`$?` 在錯誤位置沒展開、`>>` append 因我「重試」真的執行了 7 次把 `.gitignore` 寫成 30 行、`{ 多命令 } >> "$R"; cat "$R"` 的交錯——**反复誤判為「pty 污染／串擾」**。一旦貼上「污染」標籤，我就不再相信工具輸出（唯一的事實來源），於是反复重跑、查無謂的 git 歷史、**差點 `git checkout origin/main` 覆蓋檔案**（被使用者打斷）、最後把自己製造的混亂包裝成「環境不可信」甩給使用者決策。**環境從頭到尾完全正常。**

**根因＝外部歸因偏誤**：遇到非預期輸出，第一反應是怪環境，而不是先懷疑自己的命令／邏輯。把自己的 bug 投射成外部故障，然後拒絕相信現實。

固定做法（順序不可顛倒）：
1. **「污染」幾乎永遠是錯的解釋——從解釋庫裡刪掉它**。看到重複行、`$?` 沒展開、錯位，第一假設永遠是「**我的命令寫錯了**」：管線 exit code 取錯位、append 重複執行、複雜結構交錯。
2. **用最簡單的單一命令證偽**：一個 `wc -l file`、一次 `Read` 就能戳破「30 行是污染」的幻覺。你永遠有能力用一條乾淨命令確認真相——先做這個，再下任何結論。
3. **命令要簡單、一次一個目的**：避免 `{ 多命令; 迴圈; heredoc } >> "$R" 2>&1; cat "$R"` 這種結構，它本身製造交錯/重複，正是我誤判的來源。寧可多跑幾條短命令，直接輸出。
4. **不信任 ≠ 可以繞過**：絕不基於「我覺得輸出不可信」去做破壞性操作（覆蓋/reset/push）。不可信就**停下來用簡單命令查清**，而不是繞過事實去賭。
5. **自證對應 + 排除假綠**（仍適用）：輸出的檔名/行號要能回指本次輸入；「全放行」配一個反向黑樣本對照證明真判別力。但這是在「已相信輸出真實」之後的查核，不是把真實輸出當污染的藉口。

### 掃描類腳本的範圍一致性
`pre-commit --all-files` 只掃 **git 追蹤檔**；本地/CI 直接 `bash scan.sh <dir>` 會掃**所有檔含 untracked**。兩端會分歧——untracked 違規檔（如誤放 docs/ 的臨時樣本）會 pre-commit 綠、CI 紅。對策：臨時檔一律放 `$TMPDIR`，不要落在被掃描目錄；驗證收尾用 `git status <dir>` 確認無殘留。

### shell 偵測腳本的可攜性
沿用 rg→grep fallback 範式時，正則限 **ERE**，禁用 lookbehind/PCRE（grep `-P` 非 GNU 環境沒有）。fallback 環境可能連 `sed`/`awk` 都沒有——剝字串改用純 grep（如「白名單優先交替、`grep -oE` 抽片段」），別引入 sed 破壞可攜性。

## 向高級工程師學習（跨任務通用的協作習慣）

從與高工的協作中，值得我之後固定沿用：

1. **把錯誤攔在最便宜的階段**。設計評審時就點出不可攜寫法（lookbehind/PCRE 與 fallback 不相容），不讓它流到實作才返工。動工前先確認引擎/邊界假設成立，比寫完再測省得多。
2. **審查不只看配置，要親自實跑行為**。他不靠讀 YAML 下結論，而是親手跑黑/白樣本，因此抓到「同行黑+白」漏報這種純靜態看不出的缺陷。我交付與複核時也要實跑，不靠「看起來對」。
3. **區分「任務範圍內」與「跟進待辦」**。當前接點可核可上線，同時把範圍外的缺陷明確列為移交待辦——不阻擋進度，也不讓問題消失。下結論時把「這條過了」和「但這幾項要記著」分開講清楚。
4. **對自己的判斷也保持懷疑（元認知）**。最關鍵的一點：他承認「這次攔住純屬運氣」，把僥倖提煉成可執行硬規則（驗證須自證對應…），而不是自我安慰。沿用：問「下次同類問題，現有流程能穩定攔住嗎？」若答案是否，就補規則而非賭運氣。
5. **誠實暴露流程缺陷，不粉飾**。「誠實說：不能」比一句「應該沒問題」有價值得多——把不確定與漏洞講出來，團隊才能補。
