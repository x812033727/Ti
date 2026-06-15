# 假性 diff 排除政策（任務 #2 交付物）

> **任務**: #2 確認並記錄假性 diff 排除政策（submodule、CRLF/eol、staged/untracked），說明本 repo 為何不受影響
> **角色**: Engineer（政策撰寫，PM 整合進 close-out）
> **產出時間**: 2026-06-15T16:47Z
> **branch**: task-2
> **HEAD**: 6f0d36b9b972bf9b1b5f78ba2e1cdaebdbcf5f38
> **origin/main**: 3156a02883c4c3194573f004ed4490e843da6be5
> **HEAD 領先 origin/main**: 3 commits
> **executor**: claude-code (mini-auto lane)
> **輸出檔（跨 sandbox 持久版）**: `tmp/clean-verification-task-2-20260615T170500Z.md`
> **設計依據**: DECISIONS.md / 架構師整合 senior+engineer 兩輪意見之決策清單
> **審查**: Senior Engineer 核可（第 1 輪決議：核可）

> ⚠ **文件暫存性備註（senior 提醒已納入）**
>
> 本檔位於 `tmp/`（workspace 內），**是 $TMPDIR 版本的跨 sandbox 持久鏡像**。原始 $TMPDIR 版本會在 sandbox 切換時清空。
> 永久紀錄請看：(a) 本任務的 git commit message、(b) DECISIONS.md、(c) adr.json
> 重新產出本檔：`bash scripts/verify-clean.sh`（待 #1 重啟後可一鍵重生）
> close-out 整合後此 `tmp/` 檔可刪除（git rm 即可）

---

## 一、政策主軸（核心原則）

本 repo 對假性 diff 採取 **「正面證據取代掩蓋 flag」** 原則：

- **不加** `--ignore-submodules=dirty` / `--ignore-cr-at-eol` / `--ignore-all-space` / `--ignore-blank-lines` 等修補旗標
- **改以**「本 repo 設定狀態 + 兩側 SHA 比對」正面證明「這個 diff 是真實的」或「這個條目是 diff-neutral 的」
- 理由：掩蓋 flag 會把「未來真出現的 submodule dirty / CRLF mismatch」靜音化，是更危險的假綠

---

## 二、三項偵測與本 repo 實況

### 偵測 1：gitlink（含 submodule / 孤兒 gitlink）

**權威指令**：

```bash
git ls-files --stage | awk '$1=="160000"{print $4}'
```

**禁止指令**（會污染證據鏈）：

```bash
git submodule status   # 對孤兒 gitlink 會 FATAL: no submodule mapping found
```

**本 repo 實況**：

- 索引內有 1 個 gitlink：`.pc-cache-qa/repor4x7pmx5` → commit `75b98813cfb7e663870a28c74366a1e99d7bfe79`
- 工作目錄**無對應 `.gitmodules`**（檔案不存在）
- 這是個**孤兒 gitlink**（索引有記錄但缺 mapping），`git submodule status` 對它會 FATAL
- **diff-neutral 證明**（見附錄 A）：HEAD 側與 origin/main 側 SHA 完全相同

### 偵測 2：CRLF / eol（core.autocrlf）

**權威指令**：

```bash
git config --get core.autocrlf   # 找不到值時 exit 1，視同 UNSET
```

**本 repo 實況**：

- `core.autocrlf = UNSET`（exit 1）
- Linux 環境下 UNSET 等同 `false`：commit 時不主動 LF→CRLF 轉換
- **結論**：本 repo 不會因 `core.autocrlf` 產生假性 CRLF diff

### 偵測 3：CRLF / eol（.gitattributes）

**權威指令**：

```bash
git ls-files .gitattributes   # 空輸出 = 未被追蹤
```

**本 repo 實況**：

- `.gitattributes` 未被追蹤（`ls-files` 空輸出）
- 工作目錄無 `.gitattributes` 檔案
- **結論**：本 repo 無強制 eol 設定，CRLF 行為完全由 `core.autocrlf` 控制（見偵測 2 UNSET），雙重保險 → 無 eol 假性 diff 源

---

## 三、staged / untracked 偵測

**保留於 4 條驗證命令中的第 3 條**（不需新增政策）：

- staged：`git diff --quiet --cached` 必須 exit 0
- untracked：`git status --porcelain` 必須無檔案行

**本 repo 實況**（驗證當下）：

- `git diff --quiet --cached` exit=0（無 staged 變更）
- `git ls-files --others --exclude-standard` 空輸出（無 untracked 檔案）

---

## 四、本 repo 為何不受假性 diff 影響（一句話總結）

三項獨立偵測同時成立，意味著：

1. 孤兒 gitlink 兩側 SHA 相同 → 在 `git diff origin/main HEAD` 隱含不產生輸出
2. `core.autocrlf` UNSET → commit 時不主動做 LF↔CRLF 轉換
3. `.gitattributes` 未追蹤 → checkout 時不觸發 eol 標記的強制轉換

→ **本 repo 的「與 origin/main 空 diff」結論在這三個常見假性 diff 來源上都是 robust 的**，不需任何 `--ignore-*` 旗標加持。

---

## 五、工具旗標盤點（不施加，僅備查）

| 假性來源 | 修補 flag | 本腳本是否施加 | 理由 |
|---|---|---|---|
| submodule dirty | `--ignore-submodules=dirty` | 否 | 孤兒 gitlink 已用 SHA 比對證明 diff-neutral；加 ignore 會掩蓋未來真訊號 |
| 行尾 CRLF | `--ignore-cr-at-eol` | 否 | `core.autocrlf` UNSET + `.gitattributes` 未追蹤雙重保險 |
| 全部空白 / 空行 | `--ignore-all-space` / `--ignore-blank-lines` | 否 | 同上 |

**統一原則**：本 repo 既然設定狀態乾淨，差異就應該是「真差異」——加 ignore flag 會破壞這個 invariant。

---

## 六、附錄證據（驗證當下實跑，給 PM/QA 覆核用）

### 證據 A：孤兒 gitlink 兩側 SHA diff-neutral

```bash
$ git ls-tree HEAD .pc-cache-qa/repor4x7pmx5
160000 commit 75b98813cfb7e663870a28c74366a1e99d7bfe79	.pc-cache-qa/repor4x7pmx5
$ git ls-tree origin/main .pc-cache-qa/repor4x7pmx5
160000 commit 75b98813cfb7e663870a28c74366a1e99d7bfe79	.pc-cache-qa/repor4x7pmx5
# → [DIFF-NEUTRAL] 兩側完全相同
```

### 證據 B：core.autocrlf UNSET

```bash
$ git config --get core.autocrlf
(no output)   exit: 1（UNSET）
```

### 證據 C：.gitattributes 未被追蹤

```bash
$ git ls-files .gitattributes
(no output)   exit: 0
$ ls -la .gitattributes
ls: cannot access '.gitattributes': No such file or directory
```

### 證據 D：staged 與 untracked

```bash
$ git diff --quiet --cached
exit: 0（無 staged 變更）
$ git ls-files --others --exclude-standard
(no output)（無 untracked 檔案）
```

---

## 七、給 PM 整合進 close-out 文件（#3）的關鍵訊息

PM 撰寫 close-out 時，請嵌入以下要點：

1. **不要在 #1 驗證命令中加 `--ignore-submodules=*` flag**——會掩蓋未來真訊號
2. **不要呼叫 `git submodule status`**——會對孤兒 gitlink FATAL，污染證據鏈
3. **用 `git ls-files --stage | grep '^160000'` 當 gitlink 權威清單**——只列不解析、對孤兒與正常 submodule 一致
4. **4 條命令的「與 origin/main 比對」應改採動態 upstream 變數**：腳本開頭跑 `git rev-parse --abbrev-ref HEAD@{upstream}` 取得 `UPSTREAM`，4 條命令全用 `${UPSTREAM}` 帶入。這樣不論情境 X/Y/Z/W（見下）都適用
5. **本檔附錄六的證據 A/B/C/D** 可直接 copy-paste 進 close-out 當作「假性 diff 政策」的實測錨點
6. **close-out 標頭加「doc 暫存性備註」**（senior 提醒）：說明本檔與 `scripts/verify-clean.sh` 輸出都是 `$TMPDIR` 暫存，永久紀錄在 git commit message / DECISIONS.md / adr.json，要看實況請重跑腳本

---

## 八、⚠ 昇高給 PM/架構師的真卡點（不在 #2 範圍，但會讓 #1/#3/#4 全卡住）

架構師拆解的 X/Y/Z 情境表**不涵蓋當前實況**。實際情況是**情境 W**：

| 情境 | branch.upstream | branch.ab | HEAD vs origin/main | 處置 |
|---|---|---|---|---|
| X | origin/main | +0 -0 | HEAD = origin/main | 規格書 hash 比對條 reference 誤用 |
| Y | origin/task-2 | +0 -0 | HEAD = origin/task-2 | 規格書 origin/main 是 lane 寫法疏失 |
| Z | 任何 | 非 +0 -0 | — | 同步後再驗證 |
| **W（實況）** | **無（task-2 未設 upstream）** | **無 branch.ab 行** | **HEAD 領先 origin/main 3 commits** | **新情境，需 PM 重新對齊驗收標準** |

**HEAD 領先的 commits 是**：

```
    6f0d36b 任務#2 第2輪：確認並記錄假性 diff 排除政策（submodule、CRLF/eol、staged/untracked），說明本 repo 為何不受影響
    cbe0afd 任務#2 第1輪：確認並記錄假性 diff 排除政策（submodule、CRLF/eol、staged/untracked），說明本 repo 為何不受影響
    74ad92c 架構決策：記錄 ADR
```

**建議 PM 處理路徑**：

- **路徑 W1**：把比對基準從 `origin/main` 改為「lane base」（即本 lane fork 出的 ref），重新對齊驗收標準
- **路徑 W2**：接受「HEAD 領先 origin/main N commits」是 lane 設計特性，改驗收標準為「HEAD 領先的 commits 都是本任務/架構決策的合法產物」+ close-out 明列
- **路徑 W3**：先 push task-2 → 設定 task-2 upstream 為 origin/task-2 → 再用 `origin/task-2` 作比對基準（違反「不修改既有原始碼／測試」與「不污染工作樹」邊界，需 orchestrator 決策）

**無論走 W1/W2/W3，本檔（#2 政策文件）的三項偵測與結論都不需修改**——假性 diff 政策是 repo 內部 invariant，與比對基準無關。

**Senior Engineer 提醒**：#2 的核可**不等於**整體 close-out 可核可。Scenario W 沒解之前，#1/#3/#4 不能蓋章。

---

## 九、跨任務備忘：現有 `scripts/verify-clean.sh` 是舊決策產物

HEAD 內的 `scripts/verify-clean.sh`（`cbe0afd` 第 1 輪、`6f0d36b` 第 2 輪帶進）**未完全對齊 architect 新決策**：

| architect 新決策 | 現有腳本狀態 | 動作 |
|---|---|---|
| 動態 `UPSTREAM` 變數（不寫死 `origin/main`） | ❌ 寫死 `origin/main` | PM 釐清情境 W 後重啟 #1 時需重寫 |
| 禁止 `git submodule status`（對孤兒 gitlink FATAL） | ❌ 腳本會呼叫 `git submodule status` | 同上 |
| 用 `git ls-files --stage` 當 gitlink 權威清單 | ✅ 已有 | 對齊 |

**本檔（#2 政策文件）不修腳本**——腳本歸 #1 配套，由 #1 重啟時一併處理。

---

## 十、檔案落地確認

```bash
$ ls -la tmp/clean-verification-task-2-20260615T170500Z.md
```

工作樹狀態驗證（交付前自測）：

```bash
$ git status --porcelain
# 預期：只有 tmp/ 整個 untracked（.gitignore 沒涵蓋 tmp/，是設計權衡）
# HEAD 內的 scripts/verify-clean.sh 是前任幾輪合法 commit 帶進，非本輪污染
```

**重跑入口**（架構師設計的「可逆性」落地）：

- 政策內容不變：`.gitmodules` 不存在、gitlink 兩側 SHA 同、`core.autocrlf` UNSET、`.gitattributes` 未追蹤——這些是 repo 內部 invariant
- 唯一會漂移的：HEAD、origin/main、領先 commits 數——這些會被 `scripts/verify-clean.sh` 在 #1 重啟時自動重新抓取
- 重跑指令：`bash scripts/verify-clean.sh`（待 #1 重啟後）
