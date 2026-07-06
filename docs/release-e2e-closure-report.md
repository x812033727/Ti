# Release v0.2.0 生產 E2E 閉環報告

> 任務 #3 產物（唯一新增檔）。本報告**只引用** `docs/evidence/` 內既有 evidence 值，
> 不另算、不另存衍生雜湊；所有「重驗」段落貼的是 2026-07-06 實跑指令與原始輸出，
> 非事後改寫的宣稱。上游邊界聲明見 `docs/release-e2e-handoff.md`。

## 一、三列閉環表

| # | 閉環環節 | Evidence 檔路徑 | 該檔原 `captured_at_utc` | 關鍵勾稽值 | 本次線上重驗（2026-07-06） |
|---|---|---|---|---|---|
| 1 | 線上 release body 抓取（gh CLI＋REST 雙來源） | `docs/evidence/release-v0.2.0-online-body.json` | `2026-07-05T17:43:50Z` | `body_sha256=d1779cbbd4cf2a5b8ef403d466a2883b3d4fc1324257abb4d10455a52d0991f4`、`body_match=true`、`tag_match=true`、`url_match=true` | ✅ 成功，全項 match（線上 body 重抓後雜湊與 evidence 逐字一致，見二-1） |
| 2 | 線上 body 結構判定（Breaking 置頂＋四要素＋逃生艙） | `docs/evidence/release-v0.2.0-body-structure-verdict.json` | `2026-07-05T17:43:50Z`（沿用 #1 來源證據時戳） | `verdict=PASS`、`problems=[]`、黑樣本自證四項全翻紅 | ✅ 成功，全項 match（checker 實跑 exit 0，見二-2） |
| 3 | `release: published` 實際觸發 release-smoke | `docs/evidence/release-smoke-v0.2.0-trigger.json` | `2026-07-05T18:24:35Z` | `run_id=27905531397`、`event=release`、`status=completed`、`conclusion=success`、`path=.github/workflows/release-smoke.yml` | ✅ 成功，全項 match（gh CLI＋REST 雙路重查一致，見二-3） |

## 二、實際指令與原始輸出（實況，未改寫）

### 1. #1 線上 body 重驗（雜湊勾稽）

指令（沿用 evidence 檔內 `gh_release_view_command` 同源查詢，接雜湊）：

```bash
gh release view v0.2.0 --repo x812033727/Ti --json body --jq '.body' | sha256sum
```

原始輸出：

```
d1779cbbd4cf2a5b8ef403d466a2883b3d4fc1324257abb4d10455a52d0991f4  -
```

與 evidence 檔既有 `body_sha256` 逐字一致 → 線上 body 自 2026-07-05 抓取以來未變動。

### 2. #2 結構判定重驗（含 `PYTHONPATH=.` 補驗實況）

直接以 `python3 scripts/check_release_body_structure.py` 執行時，因 `sys.path[0]` 為
`scripts/`、找不到 `studio` 套件而失敗（實況，非宣稱）：

```
$ python3 scripts/check_release_body_structure.py
Traceback (most recent call last):
  File "/opt/ti-autopilot-work/scripts/check_release_body_structure.py", line 28, in <module>
    from studio.release_note import BREAKING_HEADING, pyproject_version
ModuleNotFoundError: No module named 'studio'
exit=1
```

補上 `PYTHONPATH=.` 後重跑（工程師回報之補驗做法，本次重現）：

```
$ PYTHONPATH=. python3 scripts/check_release_body_structure.py
== v0.2.0 線上 body 結構斷言核對 ==
證據檔：docs/evidence/release-v0.2.0-online-body.json
pyproject 版本（SSOT）：0.2.0
Breaking heading 常數：'## ⚠️ Breaking Changes'
頂部第一個頂層 `## ` 區塊：'## ⚠️ Breaking Changes'

核對通過（雙來源一致＋頂部 Breaking 置頂＋四要素齊＋逃生艙齊＋生效版本逐字對應）。
exit=0
```

與 evidence 檔 `verdict=PASS`、`problems=[]` 一致。

### 3. #3 smoke 觸發重驗（gh CLI `path` 欄位 N/A → REST 補驗實況）

gh CLI 的 `gh run view --json` **不支援** `path` 欄位（整合維運回報之 N/A 實況，本次重現）：

```
$ gh run view 27905531397 --repo x812033727/Ti --json path
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
exit=1
```

gh CLI 可得欄位重查：

```
$ gh run view 27905531397 --repo x812033727/Ti --json databaseId,event,status,conclusion,headBranch,url,workflowName,headSha,attempt,number,createdAt,updatedAt
{"attempt":1,"conclusion":"success","createdAt":"2026-06-21T13:15:45Z","databaseId":27905531397,"event":"release","headBranch":"v0.2.0","headSha":"f7715fa042c37d6d4f04da3f696176fdce9855da","number":2,"status":"completed","updatedAt":"2026-06-21T13:15:55Z","url":"https://github.com/x812033727/Ti/actions/runs/27905531397","workflowName":"Release smoke"}
```

`path` 欄位改走 REST 補驗（同 evidence 檔 `rest_endpoint`）：

```
$ gh api repos/x812033727/Ti/actions/runs/27905531397 --jq '{id,event,status,conclusion,html_url,head_branch,head_sha,name,path,run_attempt,run_number}'
{"conclusion":"success","event":"release","head_branch":"v0.2.0","head_sha":"f7715fa042c37d6d4f04da3f696176fdce9855da","html_url":"https://github.com/x812033727/Ti/actions/runs/27905531397","id":27905531397,"name":"Release smoke","path":".github/workflows/release-smoke.yml","run_attempt":1,"run_number":2,"status":"completed"}
```

`run_id=27905531397`／`event=release`／`conclusion=success`／
`path=.github/workflows/release-smoke.yml` 皆與 evidence 檔一致。`event=release` 證明該 run
由 release webhook（`release: published`）觸發，而非 push 或 `workflow_dispatch`。
另 evidence 檔保留同 tag 較早的失敗 run `27905351284`（`superseded_failure_run`），
不以後來的成功 run 掩蓋先前失敗，此處如實註記。

## 三、雜湊計算規則（僅註明，不另算）

`body_sha256` 的計算規則為：**`gh_release_view.body` 內容＋CLI 輸出結尾換行後，
取 UTF-8 編碼的 SHA-256**（即 `gh release view --json body --jq '.body'` 的 stdout 直接管入
`sha256sum` 所得——jq 輸出末尾自帶一個換行）。本報告只引用
`docs/evidence/release-v0.2.0-online-body.json` 內既有的
`body_sha256=d1779cbbd4cf2a5b8ef403d466a2883b3d4fc1324257abb4d10455a52d0991f4`，
不在報告中另算或另存任何衍生雜湊；二-1 的重驗輸出僅用於與該既有值當場比對。

## 四、結論

三證據俱全（#1 線上 body 抓取＋雜湊勾稽、#2 結構判定 `verdict=PASS`、#3 smoke 觸發
`event=release`/`conclusion=success`），且 2026-07-06 線上重驗全項 match：

**判定：v0.2.0 真實 `v*` tag-push 生產 E2E 鏈——閉環。**

範圍限定：本閉環判定僅及 v0.2.0。後續版本依 `docs/release-e2e-handoff.md`
「發佈後人工核對步驟」逐版驗證，不得以本報告冒充未來版本的生產證據。

## 五、缺口

無。
