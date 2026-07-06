# Release v0.2.0 生產 E2E 閉環報告

> 範圍：只做 `docs/evidence/` 既有證據勾稽與 2026-07-06 線上重驗，不重做發版。
> N/A 規則：若工具不可直接提供欄位，明示 `N/A` 並附補驗指令；不得合成佔位值。
> 本報告只引用 evidence 內既有勾稽值，不另存報告端衍生雜湊。

## 一、三列閉環表

| # | 閉環環節 | Evidence 檔路徑 | 原 `captured_at_utc` | 關鍵勾稽值 | 本次線上重驗 | 雜湊 / 判定規則 |
|---|---|---|---|---|---|---|
| #1 | 線上 release body 抓取（gh CLI + REST 雙來源） | `docs/evidence/release-v0.2.0-online-body.json` | `2026-07-05T17:43:50Z` | `body_sha256=d1779cbbd4cf2a5b8ef403d466a2883b3d4fc1324257abb4d10455a52d0991f4`、`body_match=true`、`tag_match=true`、`url_match=true` | 成功；線上 body 重抓後雜湊、tag、url 全項 match | 沿用 evidence 定義：`gh_release_view.body` 內容加 CLI 輸出結尾換行後取 UTF-8 SHA-256；正規化規則沿 evidence（CRLF->LF、去每行尾隨空白、去尾端空行） |
| #2 | 線上 body 結構判定（Breaking 置頂、四要素、逃生艙） | `docs/evidence/release-v0.2.0-body-structure-verdict.json` | `2026-07-05T17:43:50Z` | `verdict=PASS`、`problems=[]`、`雙來源正規化後逐字相等=true`、`頂部即 Breaking 置頂=true`、`四要素齊=true`、`生效版本逐字對應_自0.2.0起=true`、`逃生艙_TI_REQUIRE_CHOWN=warn/off=true` | 裸跑 `python3 scripts/check_release_body_structure.py` 因 import path 失敗；補驗 `PYTHONPATH=.` 後 PASS，與 evidence 一致 | 不另算雜湊；沿用 evidence 內 `verdict` / `checks` / `problems` |
| #3 | `release: published` 實際觸發 release-smoke | `docs/evidence/release-smoke-v0.2.0-trigger.json` | `2026-07-05T18:24:35Z` | `run_id=27905531397`、`event=release`、`status=completed`、`conclusion=success`、`path=.github/workflows/release-smoke.yml` | 成功；`gh run view` 可得欄位與 REST 全項 match。`path` 在 `gh run view --json` 為 `N/A`，由 REST 補驗 | 不用 hash；以 GitHub Actions run metadata 勾稽。`path` 補驗指令：`gh api repos/x812033727/Ti/actions/runs/27905531397 --jq '{id,event,status,conclusion,html_url,name,path}'` |

## 二、本次重驗實際指令與輸出

### #1 線上 release body

```bash
timeout 60 gh release view v0.2.0 --repo x812033727/Ti --json body --jq '.body' | sha256sum
```

```text
d1779cbbd4cf2a5b8ef403d466a2883b3d4fc1324257abb4d10455a52d0991f4  -
```

```bash
timeout 60 gh api repos/x812033727/Ti/releases/tags/v0.2.0 --jq '{tag_name,html_url,id,created_at,published_at}'
```

```json
{"created_at":"2026-06-21T13:15:15Z","html_url":"https://github.com/x812033727/Ti/releases/tag/v0.2.0","id":342528036,"published_at":"2026-06-21T13:15:44Z","tag_name":"v0.2.0"}
```

逐項比對輸出：

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

### #2 線上 body 結構斷言

原要求指令：

```bash
timeout 60 python3 scripts/check_release_body_structure.py
```

```text
Traceback (most recent call last):
  File "/opt/ti-autopilot-work.lanes/lane-ap7726860542-2/scripts/check_release_body_structure.py", line 28, in <module>
    from studio.release_note import BREAKING_HEADING, pyproject_version
ModuleNotFoundError: No module named 'studio'
```

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

### #3 release-smoke 觸發

`gh run view --json path` 不支援 `path` 欄位，依 N/A 規則保留失敗輸出：

```bash
timeout 60 gh run view 27905531397 --repo x812033727/Ti --json path
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

可得欄位重查：

```bash
timeout 60 gh run view 27905531397 --repo x812033727/Ti --json databaseId,event,status,conclusion,headBranch,url,createdAt,updatedAt,workflowName,displayTitle,headSha,number,name,attempt,startedAt
```

```json
{"attempt":1,"conclusion":"success","createdAt":"2026-06-21T13:15:45Z","databaseId":27905531397,"displayTitle":"v0.2.0","event":"release","headBranch":"v0.2.0","headSha":"f7715fa042c37d6d4f04da3f696176fdce9855da","name":"Release smoke","number":2,"startedAt":"2026-06-21T13:15:45Z","status":"completed","updatedAt":"2026-06-21T13:15:55Z","url":"https://github.com/x812033727/Ti/actions/runs/27905531397","workflowName":"Release smoke"}
```

REST 補驗 `path`：

```bash
timeout 60 gh api repos/x812033727/Ti/actions/runs/27905531397 --jq '{id,event,status,conclusion,html_url,created_at,updated_at,head_branch,head_sha,name,path,run_attempt,run_number,workflow_id,display_title}'
```

```json
{"conclusion":"success","created_at":"2026-06-21T13:15:45Z","display_title":"v0.2.0","event":"release","head_branch":"v0.2.0","head_sha":"f7715fa042c37d6d4f04da3f696176fdce9855da","html_url":"https://github.com/x812033727/Ti/actions/runs/27905531397","id":27905531397,"name":"Release smoke","path":".github/workflows/release-smoke.yml","run_attempt":1,"run_number":2,"status":"completed","updated_at":"2026-06-21T13:15:55Z","workflow_id":296211954}
```

逐項比對輸出：

```json
{
  "expected": {
    "run_id": "27905531397",
    "event": "release",
    "status": "completed",
    "conclusion": "success"
  },
  "checks": {
    "gh_run_view.run_id": true,
    "gh_run_view.event": true,
    "gh_run_view.status": true,
    "gh_run_view.conclusion": true,
    "rest_run.id": true,
    "rest_run.event": true,
    "rest_run.status": true,
    "rest_run.conclusion": true
  },
  "all_match": true
}
```

補充：evidence 檔保留同 tag 較早的失敗 run `27905351284`（`superseded_failure_run`），本報告不以後來成功 run 掩蓋先前失敗；目前閉環只採用 `run_id=27905531397` 的成功 release run。

## 三、雜湊計算規則

`body_sha256` 計算規則沿用 `docs/evidence/release-v0.2.0-online-body.json`：以 `gh_release_view.body` 內容加 CLI 輸出結尾換行後，取 UTF-8 SHA-256，等同 `gh release view v0.2.0 --repo x812033727/Ti --json body --jq '.body' | sha256sum`。

本報告只引用 evidence 內既有 `body_sha256=d1779cbbd4cf2a5b8ef403d466a2883b3d4fc1324257abb4d10455a52d0991f4`，不新增其他報告端衍生雜湊。

## 四、結論

三證據俱全：#1 線上 body 抓取與雜湊勾稽、#2 結構判定 `verdict=PASS`、#3 smoke run `event=release` / `conclusion=success`。2026-07-06 線上重驗全項 match。

**判定：v0.2.0 真實 `v*` tag-push 生產 E2E 鏈閉環。**

範圍限定：本閉環判定只及 v0.2.0；後續版本需依 `docs/release-e2e-handoff.md` 逐版驗證。

## 五、缺口

無。
