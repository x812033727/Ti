# Release v0.2.0 生產 E2E 閉環報告

> 範圍：只做 `docs/evidence/` 既有證據勾稽與 2026-07-06 線上重驗，不重做發版。
> N/A 規則：若工具不可直接提供欄位，明示 `N/A` 並附補驗指令；不得合成佔位值。
> 本報告引用 evidence 內既有勾稽值；唯一報告端另算值為 2026-07-06 線上重驗的 exact body hash
> （body 逐字、不加結尾換行；可由 evidence 內存 body 重算導出，見「三、雜湊計算規則」與「五、缺口」）。
> 第二章資料源為任務 #1／#2／#3 執行紀錄；原始路徑反映來源環境，未改寫成目前 lane 路徑。

## 一、三列閉環表

| # | 閉環環節 | Evidence 檔路徑 | 原 `captured_at_utc` | 關鍵勾稽值 | 本次線上重驗 | 雜湊 / 判定規則 |
|---|---|---|---|---|---|---|
| #1 | 線上 release body 抓取（gh CLI + REST 雙來源） | `docs/evidence/release-v0.2.0-online-body.json` | `2026-07-05T17:43:50Z` | `body_sha256=d1779cbbd4cf2a5b8ef403d466a2883b3d4fc1324257abb4d10455a52d0991f4`、`body_match=true`、`tag_match=true`、`url_match=true` | 擷取日期 2026-07-06，本次線上重驗：成功。tagName MATCH、url MATCH、body 文字 MATCH（gh vs REST vs evidence 三方逐字一致）；mismatch 落字：線上 body 逐字 exact hash `fd9b16d23eccafbd38d0d641585a025e2f77d98c8bce155b6d5a40648bf80dd4` ≠ evidence `body_sha256`——屬勾稽值計算方式瑕疵而非內容漂移，見「五、缺口」 | 沿用 evidence 定義：取 `gh_release_view.body` 內容，加 CLI 輸出結尾換行後取 UTF-8 SHA-256；正規化規則沿 evidence（CRLF->LF、去每行尾隨空白、去尾端空行） |
| #2 | 線上 body 結構判定（Breaking 置頂、四要素、逃生艙） | `docs/evidence/release-v0.2.0-body-structure-verdict.json` | `2026-07-05T17:43:50Z` | `verdict=PASS`、`problems=[]`、`雙來源正規化後逐字相等=true`、`頂部即 Breaking 置頂=true`、`四要素齊=true`、`生效版本逐字對應_自0.2.0起=true`、`逃生艙_TI_REQUIRE_CHOWN=warn/off=true` | 擷取日期 2026-07-06，本次線上重驗：成功，六項 checks 全 MATCH、exit code 0，無 mismatch。裸跑 `python3 scripts/check_release_body_structure.py` 因 import path 失敗；補驗 `PYTHONPATH=.` 後 PASS，與 evidence 一致 | 不另算雜湊；沿用 evidence 內 `verdict` / `checks` / `problems` |
| #3 | `release: published` 實際觸發 release-smoke | `docs/evidence/release-smoke-v0.2.0-trigger.json` | `2026-07-05T18:24:35Z` | `run_id=27905531397`、`event=release`、`status=completed`、`conclusion=success`、`workflow_path=.github/workflows/release-smoke.yml` | 擷取日期 2026-07-06，本次線上重驗：成功，`gh run view` 可得欄位與 REST 全項 MATCH，無 mismatch。`path` 在 `gh run view --json` 為 `N/A`，由 REST 補驗 | 不用 hash；以 GitHub Actions run metadata 勾稽。`path` 補驗指令：`gh api repos/x812033727/Ti/actions/runs/27905531397 --jq '{id,event,status,conclusion,html_url,name,path}'` |

## 二、任務 #1／#2／#3 執行紀錄轉錄

### #1 線上 release body（來源：任務 #1 執行紀錄，2026-07-06）

指令：

```bash
timeout 60 gh release view v0.2.0 --json body,tagName,url
```

原始輸出：

```json
{"body":"# Release 0.2.0\n\n## ⚠️ Breaking Changes\n\n> 獨立頂層區塊，彙整所有破壞性變更；各版本節內亦保留對應摘要供版本歷史檢索。\n\n### `TI_REQUIRE_CHOWN` 預設已改為 `strict`（自 `0.2.0` 起）\n\n- **① 行為變動**：state 檔案（history meta/events、`backlog.json`）的安全寫入模式\n  `TI_REQUIRE_CHOWN` **已改為 `strict` 預設**。寫入後會驗證檔案 owner 為 `root`（uid 0）且\n  `nlink=1`，任一不符即整體失敗、不落地半截檔。**只在以 `root` 執行的部署下會直接成功**。\n- **② 原因**：防止 symlink 攻擊與非 root 程序竄改 state，確保僅 root-only（uid 0、nlink=1）\n  路徑能寫入；舊版隱含放行會讓被竄改或半截的 state 檔靜默落地，屬安全強化而非降級。\n- **③ before / after 遷移範例**：以下為非 root 環境的遷移寫法。\n\n  之前（`0.1.x`，未顯式設定即隱含放行）：\n\n  ```bash\n  # 不設定，state 寫入不驗 owner，非 root 也能落地\n  python3 -m studio ...\n  ```\n\n  之後（`0.2.0`，`strict` 已成預設；非 root 須顯式選擇逃生艙）：\n\n  ```bash\n  # 非 root 部署：過渡期放行但記 warning\n  export TI_REQUIRE_CHOWN=warn\n  # 或完全停用 owner 驗證\n  export TI_REQUIRE_CHOWN=off\n  python3 -m studio ...\n  ```\n\n- **④ 生效版本**：自 `0.2.0` 起生效（即本版，非未來版本；`strict` 已是當前預設行為）。\n\n**三態與逃生艙**：`strict`（預設，安全側）／`warn`（過渡，放行但警告）／`off`（停用驗證）。\n若為**非 root** 環境部署，請顯式設定 `TI_REQUIRE_CHOWN=warn`（過渡）或 `off`（停用）作為使用者側逃生艙。\n\n**錯誤值 fail-safe**：無法辨識的值（如打錯字）一律 **fail-safe 回退為 `strict`** 並記 warning，\n不會靜默降級——打錯字不等於關閉驗證。\n\n**遷移指引**：完整說明見 README 的「state 安全寫入（TI_REQUIRE_CHOWN）」小節，\n以及 `.env.example` 內的 `TI_REQUIRE_CHOWN` 範例。\n\n_完整變更記錄見 CHANGELOG.md（v0.2.0）。_","tagName":"v0.2.0","url":"https://github.com/x812033727/Ti/releases/tag/v0.2.0"}
```

指令：

```bash
timeout 60 gh api repos/x812033727/Ti/releases/tags/v0.2.0 --jq '{body,tag_name,html_url,id,created_at,published_at}'
```

原始輸出：

```json
{"body":"# Release 0.2.0\n\n## ⚠️ Breaking Changes\n\n\u003e 獨立頂層區塊，彙整所有破壞性變更；各版本節內亦保留對應摘要供版本歷史檢索。\n\n### `TI_REQUIRE_CHOWN` 預設已改為 `strict`（自 `0.2.0` 起）\n\n- **① 行為變動**：state 檔案（history meta/events、`backlog.json`）的安全寫入模式\n  `TI_REQUIRE_CHOWN` **已改為 `strict` 預設**。寫入後會驗證檔案 owner 為 `root`（uid 0）且\n  `nlink=1`，任一不符即整體失敗、不落地半截檔。**只在以 `root` 執行的部署下會直接成功**。\n- **② 原因**：防止 symlink 攻擊與非 root 程序竄改 state，確保僅 root-only（uid 0、nlink=1）\n  路徑能寫入；舊版隱含放行會讓被竄改或半截的 state 檔靜默落地，屬安全強化而非降級。\n- **③ before / after 遷移範例**：以下為非 root 環境的遷移寫法。\n\n  之前（`0.1.x`，未顯式設定即隱含放行）：\n\n  ```bash\n  # 不設定，state 寫入不驗 owner，非 root 也能落地\n  python3 -m studio ...\n  ```\n\n  之後（`0.2.0`，`strict` 已成預設；非 root 須顯式選擇逃生艙）：\n\n  ```bash\n  # 非 root 部署：過渡期放行但記 warning\n  export TI_REQUIRE_CHOWN=warn\n  # 或完全停用 owner 驗證\n  export TI_REQUIRE_CHOWN=off\n  python3 -m studio ...\n  ```\n\n- **④ 生效版本**：自 `0.2.0` 起生效（即本版，非未來版本；`strict` 已是當前預設行為）。\n\n**三態與逃生艙**：`strict`（預設，安全側）／`warn`（過渡，放行但警告）／`off`（停用驗證）。\n若為**非 root** 環境部署，請顯式設定 `TI_REQUIRE_CHOWN=warn`（過渡）或 `off`（停用）作為使用者側逃生艙。\n\n**錯誤值 fail-safe**：無法辨識的值（如打錯字）一律 **fail-safe 回退為 `strict`** 並記 warning，\n不會靜默降級——打錯字不等於關閉驗證。\n\n**遷移指引**：完整說明見 README 的「state 安全寫入（TI_REQUIRE_CHOWN）」小節，\n以及 `.env.example` 內的 `TI_REQUIRE_CHOWN` 範例。\n\n_完整變更記錄見 CHANGELOG.md（v0.2.0）。_","created_at":"2026-06-21T13:15:15Z","html_url":"https://github.com/x812033727/Ti/releases/tag/v0.2.0","id":342528036,"published_at":"2026-06-21T13:15:44Z","tag_name":"v0.2.0"}
```

記憶體逐項比對輸出：

```json
{
  "body_sha256": {
    "actual": "d1779cbbd4cf2a5b8ef403d466a2883b3d4fc1324257abb4d10455a52d0991f4",
    "expected_from_evidence": "d1779cbbd4cf2a5b8ef403d466a2883b3d4fc1324257abb4d10455a52d0991f4",
    "matches_evidence": true
  },
  "tag_match": {
    "actual": true,
    "expected_from_evidence": true,
    "matches_evidence": true
  },
  "url_match": {
    "actual": true,
    "expected_from_evidence": true,
    "matches_evidence": true
  }
}
```

註：JSON 中 `actual` 為 2026-07-06 重驗 actual，非新增勾稽值；`expected_from_evidence` 引自 evidence 既有值。

### #2 線上 body 結構斷言（來源：任務 #1 執行紀錄，2026-07-06）

原要求指令（裸跑，因缺 import path 失敗）：

```bash
timeout 60 python3 scripts/check_release_body_structure.py
```

```text
Traceback (most recent call last):
  File "/opt/ti-autopilot-work/scripts/check_release_body_structure.py", line 28, in <module>
    from studio.release_note import BREAKING_HEADING, pyproject_version
ModuleNotFoundError: No module named 'studio'
```

> 路徑反映上游任務執行環境（`/opt/ti-autopilot-work`），非本 lane；為原始出處，未改寫。

補驗指令：

```bash
timeout 60 env PYTHONPATH=. python3 scripts/check_release_body_structure.py
```

```text
== v0.2.0 線上 body 結構斷言核對 ==
證據檔：docs/evidence/release-v0.2.0-online-body.json
pyproject 版本（SSOT）：0.2.0
Breaking heading 常數：'## ⚠️ Breaking Changes'
頂部第一個頂層 `## ` 區塊：'## ⚠️ Breaking Changes'

核對通過（雙來源一致＋頂部 Breaking 置頂＋四要素齊＋逃生艙齊＋生效版本逐字對應）。
```

逐項比對輸出：

```json
{
  "verdict": {
    "actual": "PASS",
    "expected_from_evidence": "PASS",
    "matches_evidence": true
  },
  "checks": {
    "雙來源正規化後逐字相等(gh vs REST)": true,
    "頂部第一個頂層## 區塊": "## ⚠️ Breaking Changes",
    "頂部即 Breaking 置頂": true,
    "四要素齊(①行為變動②原因③before/after④生效版本)": true,
    "生效版本逐字對應_自0.2.0起": true,
    "逃生艙_TI_REQUIRE_CHOWN=warn/off": true
  }
}
```

### #3 release-smoke 觸發（來源：任務 #2 執行紀錄，2026-07-06）

`gh run view --json path` 不支援 `path` 欄位，依 N/A 規則保留失敗輸出：

```bash
gh run view 27905531397 --json event,status,conclusion,workflowName,path,url
```

```text
Unknown JSON field: "path"
Available fields:
  attempt
  conclusion
  createdAt
  databaseId
  displayTitle
  event
  headBranch
  headSha
  jobs
  name
  number
  startedAt
  status
  updatedAt
  url
  workflowDatabaseId
  workflowName
```

REST 補驗：

```bash
gh api repos/x812033727/Ti/actions/runs/27905531397 --jq '{event,status,conclusion,name,path,html_url}'
```

```json
{"conclusion":"success","event":"release","html_url":"https://github.com/x812033727/Ti/actions/runs/27905531397","name":"Release smoke","path":".github/workflows/release-smoke.yml","status":"completed"}
```

補驗指令：

```bash
gh run view 27905531397 --json databaseId,event,status,conclusion,workflowName,url
```

```json
{"conclusion":"success","databaseId":27905531397,"event":"release","status":"completed","url":"https://github.com/x812033727/Ti/actions/runs/27905531397","workflowName":"Release smoke"}
```

REST 補驗 run_id/path：

```bash
gh api repos/x812033727/Ti/actions/runs/27905531397 --jq '{id,event,status,conclusion,html_url,name,path}'
```

```json
{"conclusion":"success","event":"release","html_url":"https://github.com/x812033727/Ti/actions/runs/27905531397","id":27905531397,"name":"Release smoke","path":".github/workflows/release-smoke.yml","status":"completed"}
```

逐項比對：

| 項目 | actual | expected_from_evidence | 結果 |
|---|---:|---:|---|
| run_id | `27905531397` | `27905531397` | match |
| event | `release` | `release` | match |
| status | `completed` | `completed` | match |
| conclusion | `success` | `success` | match |
| workflow_path | `.github/workflows/release-smoke.yml` | `.github/workflows/release-smoke.yml` | match |
| gh run view `path` | N/A：目前 GH CLI 不支援 `path` 欄位 | `.github/workflows/release-smoke.yml` | 以 REST 補驗 |

補充：evidence 檔保留同 tag 較早的失敗 run `27905351284`（`superseded_failure_run`），本報告不以後來成功 run 掩蓋先前失敗；目前閉環只採用 `run_id=27905531397` 的成功 release run。

### 2026-07-06 線上重驗可照抄重跑指令（qa／工程師提供）

關鍵值一律內嵌於本節與三列表，**不以 `$TMPDIR` 路徑作為唯一證據**（`$TMPDIR` 產物僅為輔助落檔，可隨環境消失）。

#1 gh CLI + REST 雙來源 raw 落檔與 identity 欄位比對（重驗結果：tagName MATCH、url MATCH、body_sha256 MISMATCH；易變欄位只記錄不比對）：

```bash
set -euo pipefail
TMP="${TMPDIR:-/tmp}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
GH="$TMP/task1-gh-release-view-v0.2.0-$STAMP.json"
REST="$TMP/task1-rest-release-by-tag-v0.2.0-$STAMP.json"
EVIDENCE="docs/evidence/release-v0.2.0-online-body.json"
EVIDENCE_ID="$TMP/task1-evidence-identity-v0.2.0-$STAMP.json"
GH_ID="$TMP/task1-gh-identity-v0.2.0-$STAMP.json"
REST_ID="$TMP/task1-rest-identity-v0.2.0-$STAMP.json"

timeout 60 gh auth status
timeout 60 curl -sf https://api.github.com/rate_limit >/dev/null
timeout 60 gh release view v0.2.0 --json tagName,url --repo x812033727/Ti >/dev/null
timeout 60 gh release view v0.2.0 --json body,tagName,url --repo x812033727/Ti >"$GH"
timeout 60 gh api repos/x812033727/Ti/releases/tags/v0.2.0 >"$REST"

GH_SHA="$(jq -e -rj '.body // ""' "$GH" | sha256sum | awk '{print $1}')"
REST_SHA="$(jq -e -rj '.body // ""' "$REST" | sha256sum | awk '{print $1}')"

jq -e -S '{tagName:.gh_release_view.tagName,url:.gh_release_view.url,body_sha256:.body_sha256}' \
  "$EVIDENCE" >"$EVIDENCE_ID"
jq -e -S --arg body_sha256 "$GH_SHA" \
  '{tagName:.tagName,url:.url,body_sha256:$body_sha256}' \
  "$GH" >"$GH_ID"
jq -e -S --arg body_sha256 "$REST_SHA" \
  '{tagName:.tag_name,url:.html_url,body_sha256:$body_sha256}' \
  "$REST" >"$REST_ID"

diff -u "$EVIDENCE_ID" "$GH_ID" || true
diff -u "$EVIDENCE_ID" "$REST_ID" || true
jq -e '{id,created_at,published_at}' "$REST"
```

`diff -u` 的唯一差異為 `body_sha256`：線上 gh/REST exact body hash 皆為 `fd9b16d23eccafbd38d0d641585a025e2f77d98c8bce155b6d5a40648bf80dd4`，evidence 為 `d1779cbbd4cf2a5b8ef403d466a2883b3d4fc1324257abb4d10455a52d0991f4`。兩者差一個結尾換行字元——此即「五、缺口」所述計算方式瑕疵的最小重現。

#2 結構斷言（重驗結果：六項 checks 全 MATCH、exit code 0）：

```bash
timeout 60 env PYTHONPATH=. python3 scripts/check_release_body_structure.py; echo "exit=$?"
```

預期尾行：`exit=0`（本 repo 內亦可用 `.venv/bin/python scripts/check_release_body_structure.py`）。

#3 smoke run 勾稽（重驗結果：全項 MATCH）：

```bash
timeout 60 gh run view 27905531397 --json databaseId,event,status,conclusion,workflowName,url
timeout 60 gh api repos/x812033727/Ti/actions/runs/27905531397 --jq '{id,event,status,conclusion,html_url,name,path}'
```

## 三、雜湊計算規則

`body_sha256` 的計算規則沿用 `docs/evidence/release-v0.2.0-online-body.json` 內定義：取 `gh_release_view.body` 字串、保留 CLI 輸出結尾換行、以 UTF-8 取 SHA-256；正規化規則沿 evidence（CRLF->LF、去每行尾隨空白、去尾端空行）。2026-07-06 重驗已證實：此規則實為 `jq -r` 式 raw 輸出**含結尾換行**的 hash，非 body 逐字 exact hash（見「五、缺口」）。

本報告引用的雜湊只有兩個，皆可由 evidence 反查或重算導出：

- evidence 既有勾稽值 `body_sha256=d1779cbbd4cf2a5b8ef403d466a2883b3d4fc1324257abb4d10455a52d0991f4`（body + 結尾換行）。
- 2026-07-06 重驗 exact body hash `fd9b16d23eccafbd38d0d641585a025e2f77d98c8bce155b6d5a40648bf80dd4`（body 逐字、不加結尾換行；對 evidence 內存 `gh_release_view.body` 重算即得同值，故非新增外部勾稽源）。

除上述兩值外，不新增其他報告端衍生雜湊。

## 四、結論

三證據俱全：#1 線上 body 抓取與勾稽（tagName/url/body 文字 MATCH）、#2 結構判定 `verdict=PASS`（六項 checks 全 MATCH、exit code 0）、#3 smoke run `event=release` / `conclusion=success` 全項 MATCH。

降級註記：#1 的 evidence `body_sha256` 經 2026-07-06 重驗證實為計算方式瑕疵（`jq -r` 含結尾換行的 hash，非 body 逐字 hash；交叉檢查 body 文字 MATCH，非線上內容漂移，詳見「五、缺口」）。故結論由「全項無不符」**降級**為：**內容閉環成立，但 evidence 勾稽值計算方式存在已知瑕疵、修復列移交待辦**。

**判定：閉環（僅及 v0.2.0）——v0.2.0 生產 E2E 鏈內容閉環成立，附「五、缺口」所列 evidence hash 計算方式瑕疵。**

範圍限定：本閉環判定（含上述降級註記）只及 v0.2.0；後續版本需依 `docs/release-e2e-handoff.md` 逐版驗證。

## 五、缺口

**缺口 1（本輪唯一缺口）：evidence `body_sha256` 為 `jq -r` 含結尾換行的 hash，非 body 逐字 exact hash。**

- 事實：`docs/evidence/release-v0.2.0-online-body.json` 的 `body_sha256=d1779cbbd4cf2a5b8ef403d466a2883b3d4fc1324257abb4d10455a52d0991f4`，經 2026-07-06 重驗證實是 `gh --jq '.body'`（`jq -r` 式 raw 輸出）**含結尾換行字元**的 SHA-256；body 逐字 exact hash 為 `fd9b16d23eccafbd38d0d641585a025e2f77d98c8bce155b6d5a40648bf80dd4`。兩者差一個 `\n`，最小重現指令見「二、2026-07-06 線上重驗可照抄重跑指令」。
- 定性：**屬勾稽值計算方式瑕疵，非線上內容漂移**——交叉檢查線上 body 文字（gh CLI vs REST vs evidence 內存 body）三方逐字 MATCH；且對 evidence 內存 body 重算 exact hash 即得 `fd9b16…`，可自證兩值指向同一份內容。
- 影響：結論相應降級（見「四、結論」降級註記），且範圍僅及 v0.2.0；不影響 #2／#3 的判定。
- 處置：**修復 evidence `body_sha256`（改為 body 逐字 exact hash 並更新計算規則描述）列為移交待辦；本場不修、不動 evidence 檔。**

## 六、交付狀態對照

本報告已入 git 追蹤；2026-07-06 重驗僅更新本檔（三列表重驗欄含 mismatch 落字、可照抄重跑指令節、雜湊計算規則章、結論降級、缺口章、本交付狀態章），未新增任何 evidence 副本，未改動任何 `docs/evidence/` 檔案；報告端雜湊僅限「三、雜湊計算規則」列出的兩值。

備註（移交待辦）：

1. 修復 evidence `body_sha256` 計算方式瑕疵（見「五、缺口」）——本場不修、不動 evidence 檔。
2. `scripts/check_release_body_structure.py` 的 `PYTHONPATH` 自舉問題維持移交待辦，不併入本輪。
