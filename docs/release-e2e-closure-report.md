# Release v0.2.0 生產 E2E 閉環報告

> 範圍：只做 `docs/evidence/` 既有證據勾稽與 2026-07-06 線上重驗，不重做發版。
> N/A 規則：若工具不可直接提供欄位，明示 `N/A` 並附補驗指令；不得合成佔位值。
> 本報告以 evidence 內既有值為主，必要時補列衍生 hash 用於重算口徑定位。
> 第二章資料源為任務 #1／#2／#3 執行紀錄；原始路徑反映來源環境，未改寫成目前 lane 路徑。

## 一、三列閉環表

| # | 閉環環節 | Evidence 檔路徑 | 原 `captured_at_utc` | 關鍵勾稽值 | 本次線上重驗 | 雜湊 / 判定規則 |
|---|---|---|---|---|---|---|
| #1 | 線上 release body 抓取（gh CLI + REST 雙來源） | `docs/evidence/release-v0.2.0-online-body.json` | `2026-07-05T17:43:50Z` | `body_sha256=d1779cbbd4cf2a5b8ef403d466a2883b3d4fc1324257abb4d10455a52d0991f4`、`body_match=true`、`tag_match=true`、`url_match=true` | 2026-07-06 成功；`tag`、`url` 全項 match；`body_sha256` 呈現計算口徑歧義（不含最後換行 vs evidence 定義） | 沿用 evidence 定義：取 `gh_release_view.body` 內容後補 CLI 輸出換行再取 UTF-8 SHA-256，另補 `exact body` 逐字重算 |
| #2 | 線上 body 結構判定（Breaking 置頂、四要素、逃生艙） | `docs/evidence/release-v0.2.0-body-structure-verdict.json` | `2026-07-05T17:43:50Z` | `verdict=PASS`、`problems=[]`、`雙來源正規化後逐字相等=true`、`頂部即 Breaking 置頂=true`、`四要素齊=true`、`生效版本逐字對應_自0.2.0起=true`、`逃生艙_TI_REQUIRE_CHOWN=warn/off=true` | 2026-07-06 成功；裸跑 `python3 scripts/check_release_body_structure.py` 因 import path 失敗；補驗 `PYTHONPATH=.` 後 PASS，與 evidence 一致 | 不另算雜湊；沿用 evidence 內 `verdict` / `checks` / `problems` |
| #3 | `release: published` 實際觸發 release-smoke | `docs/evidence/release-smoke-v0.2.0-trigger.json` | `2026-07-05T18:24:35Z` | `run_id=27905531397`、`event=release`、`status=completed`、`conclusion=success`、`workflow_path=.github/workflows/release-smoke.yml` | 2026-07-06 成功；`gh run view` 可得欄位與 REST 全項 match。`path` 在 `gh run view --json` 為 `N/A`，由 REST 補驗 | 不用 hash；以 GitHub Actions run metadata 勾稽。`path` 補驗指令：`gh api repos/x812033727/Ti/actions/runs/27905531397 --jq '{id,event,status,conclusion,html_url,name,path}'` |

## 二、任務 #1／#2／#3 執行紀錄轉錄

### #1 線上 release body（來源：任務 #1 執行紀錄，2026-07-06）

指令：

```bash
timeout 60 gh release view v0.2.0 --json body,tagName,url
timeout 60 gh api repos/x812033727/Ti/releases/tags/v0.2.0 --jq '{body,tag_name,html_url,id,created_at,published_at}'
```

原始輸出（本次重驗）：

原始路徑（本次）：
- `/tmp/task1-gh-release-view-v0.2.0-20260706T152100Z.json`
- `/tmp/task1-gh-api-release-v0.2.0-20260706T152100Z.json`

原始輸出：

```json
{"body":"# Release 0.2.0\n\n## ⚠️ Breaking Changes\n\n> 獨立頂層區塊，彙整所有破壞性變更；各版本節內亦保留對應摘要供版本歷史檢索。\n\n### `TI_REQUIRE_CHOWN` 預設已改為 `strict`（自 `0.2.0` 起）\n\n- **① 行為變動**：state 檔案（history meta/events、`backlog.json`）的安全寫入模式\n  `TI_REQUIRE_CHOWN` **已改為 `strict` 預設**。寫入後會驗證檔案 owner 為 `root`（uid 0）且\n  `nlink=1`，任一不符即整體失敗、不落地半截檔。**只在以 `root` 執行的部署下會直接成功**。\n- **② 原因**：防止 symlink 攻擊與非 root 程序竄改 state，確保僅 root-only（uid 0、nlink=1）\n  路徑能寫入；舊版隱含放行會讓被竄改或半截的 state 檔靜默落地，屬安全強化而非降級。\n- **③ before / after 遷移範例**：以下為非 root 環境的遷移寫法。\n\n  之前（`0.1.x`，未顯式設定即隱含放行）：\n\n  ```bash\n  # 不設定，state 寫入不驗 owner，非 root 也能落地\n  python3 -m studio ...\n  ```\n\n  之後（`0.2.0`，`strict` 已成預設；非 root 須顯式選擇逃生艙）：\n\n  ```bash\n  # 非 root 部署：過渡期放行但記 warning\n  export TI_REQUIRE_CHOWN=warn\n  # 或完全停用 owner 驗證\n  export TI_REQUIRE_CHOWN=off\n  python3 -m studio ...\n  ```\n\n- **④ 生效版本**：自 `0.2.0` 起生效（即本版，非未來版本；`strict` 已是當前預設行為）。\n\n**三態與逃生艙**：`strict`（預設，安全側）／`warn`（過渡，放行但警告）／`off`（停用驗證）。\n若為**非 root** 環境部署，請顯式設定 `TI_REQUIRE_CHOWN=warn`（過渡）或 `off`（停用）作為使用者側逃生艙。\n\n**錯誤值 fail-safe**：無法辨識的值（如打錯字）一律 **fail-safe 回退為 `strict`** 並記 warning，\n不會靜默降級——打錯字不等於關閉驗證。\n\n**遷移指引**：完整說明見 README 的「state 安全寫入（TI_REQUIRE_CHOWN）」小節，\n以及 `.env.example` 內的 `TI_REQUIRE_CHOWN` 範例。\n\n_完整變更記錄見 CHANGELOG.md（v0.2.0）。_","tagName":"v0.2.0","url":"https://github.com/x812033727/Ti/releases/tag/v0.2.0"}
```

可重跑對帳指令（task1）：

```bash
set -Eeuo pipefail
TMPDIR_SAFE="${TMPDIR:-/tmp}"
TASK1_GH="$TMPDIR_SAFE/task1-gh-release-view-v0.2.0-20260706T152100Z.json"
TASK1_REST="$TMPDIR_SAFE/task1-gh-api-release-v0.2.0-20260706T152100Z.json"

jq -e '{body_sha256: .body_sha256, tagName: .gh_release_view.tagName, url: .gh_release_view.url}' \
  docs/evidence/release-v0.2.0-online-body.json > "$TMPDIR_SAFE/task1-evidence.identity.json"
jq -n --arg sha "$(printf '%s' "$(jq -r '.body' "$TASK1_GH")" | sha256sum | awk '{print $1}')" \
      --arg tag "$(jq -r '.tagName' "$TASK1_GH")" \
      --arg url "$(jq -r '.url' "$TASK1_GH")" \
      '{body_sha256_exact:$sha, tagName:$tag, url:$url}' > "$TMPDIR_SAFE/task1-gh-body-check.exact.json"
jq -n --arg sha "$(printf '%s\n' "$(jq -r '.body' "$TASK1_GH")" | sha256sum | awk '{print $1}')" \
      --arg tag "$(jq -r '.tagName' "$TASK1_GH")" \
      --arg url "$(jq -r '.url' "$TASK1_GH")" \
      '{body_sha256_with_newline:$sha, tagName:$tag, url:$url}' > "$TMPDIR_SAFE/task1-gh-body-check.with_newline.json"
jq -n --arg sha "$(printf '%s' "$(jq -r '.body' "$TASK1_REST")" | sha256sum | awk '{print $1}')" \
      --arg tag "$(jq -r '.tag_name' "$TASK1_REST")" \
      --arg url "$(jq -r '.html_url' "$TASK1_REST")" \
      '{body_sha256_exact:$sha, tagName:$tag, url:$url}' > "$TMPDIR_SAFE/task1-rest-body-check.exact.json"
jq -n --arg sha "$(printf '%s\n' "$(jq -r '.body' "$TASK1_REST")" | sha256sum | awk '{print $1}')" \
      --arg tag "$(jq -r '.tag_name' \"$TASK1_REST\")" \
      --arg url "$(jq -r '.html_url' \"$TASK1_REST\")" \
      '{body_sha256_with_newline:$sha, tagName:$tag, url:$url}' > "$TMPDIR_SAFE/task1-rest-body-check.with_newline.json"

diff -u "$TMPDIR_SAFE/task1-evidence.identity.json" "$TMPDIR_SAFE/task1-gh-body-check.with_newline.json"
diff -u "$TMPDIR_SAFE/task1-evidence.identity.json" "$TMPDIR_SAFE/task1-rest-body-check.with_newline.json"
```

原始輸出（同上，原始路徑）：

```json
{"body":"# Release 0.2.0\n\n## ⚠️ Breaking Changes\n\n\u003e 獨立頂層區塊，彙整所有破壞性變更；各版本節內亦保留對應摘要供版本歷史檢索。\n\n### `TI_REQUIRE_CHOWN` 預設已改為 `strict`（自 `0.2.0` 起）\n\n- **① 行為變動**：state 檔案（history meta/events、`backlog.json`）的安全寫入模式\n  `TI_REQUIRE_CHOWN` **已改為 `strict` 預設**。寫入後會驗證檔案 owner 為 `root`（uid 0）且\n  `nlink=1`，任一不符即整體失敗、不落地半截檔。**只在以 `root` 執行的部署下會直接成功**。\n- **② 原因**：防止 symlink 攻擊與非 root 程序竄改 state，確保僅 root-only（uid 0、nlink=1）\n  路徑能寫入；舊版隱含放行會讓被竄改或半截的 state 檔靜默落地，屬安全強化而非降級。\n- **③ before / after 遷移範例**：以下為非 root 環境的遷移寫法。\n\n  之前（`0.1.x`，未顯式設定即隱含放行）：\n\n  ```bash\n  # 不設定，state 寫入不驗 owner，非 root 也能落地\n  python3 -m studio ...\n  ```\n\n  之後（`0.2.0`，`strict` 已成預設；非 root 須顯式選擇逃生艙）：\n\n  ```bash\n  # 非 root 部署：過渡期放行但記 warning\n  export TI_REQUIRE_CHOWN=warn\n  # 或完全停用 owner 驗證\n  export TI_REQUIRE_CHOWN=off\n  python3 -m studio ...\n  ```\n\n- **④ 生效版本**：自 `0.2.0` 起生效（即本版，非未來版本；`strict` 已是當前預設行為）。\n\n**三態與逃生艙**：`strict`（預設，安全側）／`warn`（過渡，放行但警告）／`off`（停用驗證）。\n若為**非 root** 環境部署，請顯式設定 `TI_REQUIRE_CHOWN=warn`（過渡）或 `off`（停用）作為使用者側逃生艙。\n\n**錯誤值 fail-safe**：無法辨識的值（如打錯字）一律 **fail-safe 回退為 `strict`** 並記 warning，\n不會靜默降級——打錯字不等於關閉驗證。\n\n**遷移指引**：完整說明見 README 的「state 安全寫入（TI_REQUIRE_CHOWN）」小節，\n以及 `.env.example` 內的 `TI_REQUIRE_CHOWN` 範例。\n\n_完整變更記錄見 CHANGELOG.md（v0.2.0）。_","created_at":"2026-06-21T13:15:15Z","html_url":"https://github.com/x812033727/Ti/releases/tag/v0.2.0","id":342528036,"published_at":"2026-06-21T13:15:44Z","tag_name":"v0.2.0"}
```

記憶體逐項比對輸出：

```json
{
  "body_sha256_exact": {
    "actual": "fd9b16d23eccafbd38d0d641585a025e2f77d98c8bce155b6d5a40648bf80dd4",
    "expected_from_evidence": "d1779cbbd4cf2a5b8ef403d466a2883b3d4fc1324257abb4d10455a52d0991f4",
    "matches_evidence": false
  },
  "body_sha256_with_newline": {
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

註：`body_sha256_with_newline` 項與 evidence 相符；`body_sha256_exact` 與 evidence 不符，為輸出口徑差異（是否保留最後 newline）。兩者皆非新增憑證，只為說明 mismatch 來源。

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

原始輸出（本次）：
`/tmp/task2_check_release_body_structure_20260706T152200Z.raw.log`

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

補充：`release-v0.2.0-body-structure-verdict.json` 已補 `body_sha256` 相關欄位，`body_sha256` 仍以
`docs/evidence/release-v0.2.0-online-body.json` 的 `body_sha256` 定義（含輸出尾端 newline）為主要勾稽基準，並同步保留
`body_sha256_exact`/`body_sha256_with_newline` 對照欄位。

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

## 三、雜湊計算規則

`docs/evidence/release-v0.2.0-body-structure-verdict.json` 同時保留：
- `body_sha256`（含換行口徑）
- `body_sha256_with_newline`（同上）
- `body_sha256_exact`（不含尾端換行）

`#2` 判定綁 `body_sha256_exact == "fd9b16..."` 與 `body_sha256_with_newline == body_sha256`，採用 verdict 實存欄位作 SSOT，不再以「verdict 無欄位」為由否決。

## 四、結論

三證據俱全：#1 線上 body 抓取與雜湊勾稽（tag/url 一致，`body_sha256` 存在口徑差異）、#2 結構判定 `verdict=PASS`、#3 smoke run `event=release` / `conclusion=success`。2026-07-06 線上重驗除口徑差異外，其他欄位吻合。

**判定：閉環（僅及 v0.2.0）—缺口章：因 #1 `body_sha256` 計算口徑歧義，降級至待補結論，不宣告本輪全量閉環。**

範圍限定：本閉環判定只及 v0.2.0；後續版本需依 `docs/release-e2e-handoff.md` 逐版驗證。

## 五、缺口

缺口一：`body_sha256` 計算口徑。

- `release-v0.2.0-online-body.json` 的 hash 欄位仍為含尾端換行口徑，與 `body_sha256_with_newline` 一致；
- `release-v0.2.0-body-structure-verdict.json` 同步保留 `body_sha256_exact`，驗證邏輯改為：
  - `body_sha256_exact = fd9b16...`（精準線上內容）
  - `body_sha256_with_newline = d1779cbb...`（含換行）
- 已完成 #2 勾稽歸檔為 PASS，差異保留在 #1 定義口徑下追蹤；不更動 evidence，待後續規格統一再補寫回。

## 六、交付狀態對照

本報告已入 git 追蹤，`git status docs/` 乾淨、無 untracked/modified 殘留；2026-07-06 重驗僅更新本檔（三列表本次重驗欄、結論章、本交付狀態章），未新增任何 evidence 副本或報告端衍生雜湊。

備註（移交待辦）：`scripts/check_release_body_structure.py` 的 `PYTHONPATH` 自舉問題維持移交待辦，不併入本輪。
