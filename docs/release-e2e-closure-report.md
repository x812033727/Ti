# Release E2E 閉環報告（v0.2.0）

> 範圍：只做 evidence 勾稽與線上重驗，不在報告端重算新雜湊。  
> N/A 規則：若某工具不可直接提供欄位，就明示 `N/A`，並附補驗指令。

## 三列閉環表

| # | evidence 檔 | 勾稽值 | captured_at_utc | 本次重驗結果 | 雜湊 / 判定規則 |
|---|---|---|---|---|---|
| #1 | `docs/evidence/release-v0.2.0-online-body.json` | `body_sha256=d1779cbbd4cf2a5b8ef403d466a2883b3d4fc1324257abb4d10455a52d0991f4`、`tag=v0.2.0`、`url=https://github.com/x812033727/Ti/releases/tag/v0.2.0` | `2026-07-05T17:43:50Z` | `gh release view v0.2.0` 與 REST 重抓一致；`body_match=true` / `tag_match=true` / `url_match=true` | 以 `gh_release_view.body` 原文做 UTF-8 SHA-256；正規化沿 evidence：CRLF->LF、去每行尾隨空白、去尾端空行 |
| #2 | `docs/evidence/release-v0.2.0-body-structure-verdict.json` | `verdict=PASS`、`雙來源正規化後逐字相等=true`、`頂部即 Breaking 置頂=true`、`四要素齊=true`、`生效版本逐字對應_自0.2.0起=true`、`逃生艙_TI_REQUIRE_CHOWN=warn/off=true` | `2026-07-05T17:43:50Z` | 裸跑 `python3 scripts/check_release_body_structure.py` 失敗於 `ModuleNotFoundError: No module named 'studio'`；補驗 `PYTHONPATH=.` 後 PASS | 不另算報告端雜湊，只沿用 evidence verdict / checks |
| #3 | `docs/evidence/release-smoke-v0.2.0-trigger.json` | `run_id=27905531397` / `event=release` / `status=completed` / `conclusion=success` | `2026-07-05T18:24:35Z` | `gh run view 27905531397` 與 REST 重查四欄一致；`path` 在 `gh run view` 端未查，列為 `N/A`，由 REST 補得 `.github/workflows/release-smoke.yml` | 不用 hash；以 run metadata 勾稽。若要補查 `path`，用 REST `gh api repos/x812033727/Ti/actions/runs/27905531397 --jq '{id,event,status,conclusion,html_url,name,path}'` |

## 本次重驗實際指令與輸出

### 可直接重跑

執行指令: timeout 60 gh run view 27905531397 --repo x812033727/Ti --json databaseId,event,status,conclusion,headBranch,url,createdAt,updatedAt,workflowName,displayTitle,headSha,number,name,attempt,startedAt && timeout 60 gh api repos/x812033727/Ti/actions/runs/27905531397 --jq '{id,event,status,conclusion,html_url,created_at,updated_at,head_branch,head_sha,name,path,run_attempt,run_number,workflow_id,display_title}'

### #1 線上 release body

```bash
timeout 60 gh release view v0.2.0 --json body,tagName,url
```

~~~~text
{"body":"# Release 0.2.0\n\n## ⚠️ Breaking Changes\n\n> 獨立頂層區塊，彙整所有破壞性變更；各版本節內亦保留對應摘要供版本歷史檢索。\n\n### `TI_REQUIRE_CHOWN` 預設已改為 `strict`（自 `0.2.0` 起）\n\n- **① 行為變動**：state 檔案（history meta/events、`backlog.json`）的安全寫入模式\n  `TI_REQUIRE_CHOWN` **已改為 `strict` 預設**。寫入後會驗證檔案 owner 為 `root`（uid 0）且\n  `nlink=1`，任一不符即整體失敗、不落地半截檔。**只在以 `root` 執行的部署下會直接成功**。\n- **② 原因**：防止 symlink 攻擊與非 root 程序竄改 state，確保僅 root-only（uid 0、nlink=1）\n  路徑能寫入；舊版隱含放行會讓被竄改或半截的 state 檔靜默落地，屬安全強化而非降級。\n- **③ before / after 遷移範例**：以下為非 root 環境的遷移寫法。\n\n  之前（`0.1.x`，未顯式設定即隱含放行）：\n\n  ```bash\n  # 不設定，state 寫入不驗 owner，非 root 也能落地\n  python3 -m studio ...\n  ```\n\n  之後（`0.2.0`，`strict` 已成預設；非 root 須顯式選擇逃生艙）：\n\n  ```bash\n  # 非 root 部署：過渡期放行但記 warning\n  export TI_REQUIRE_CHOWN=warn\n  # 或完全停用 owner 驗證\n  export TI_REQUIRE_CHOWN=off\n  python3 -m studio ...\n  ```\n\n- **④ 生效版本**：自 `0.2.0` 起生效（即本版，非未來版本；`strict` 已是當前預設行為）。\n\n**三態與逃生艙**：`strict`（預設，安全側）／`warn`（過渡，放行但警告）／`off`（停用驗證）。\n若為**非 root** 環境部署，請顯式設定 `TI_REQUIRE_CHOWN=warn`（過渡）或 `off`（停用）作為使用者側逃生艙。\n\n**錯誤值 fail-safe**：無法辨識的值（如打錯字）一律 **fail-safe 回退為 `strict`** 並記 warning，\n不會靜默降級——打錯字不等於關閉驗證。\n\n**遷移指引**：完整說明見 README 的「state 安全寫入（TI_REQUIRE_CHOWN）」小節，\n以及 `.env.example` 內的 `TI_REQUIRE_CHOWN` 範例。\n\n_完整變更記錄見 CHANGELOG.md（v0.2.0）。_","tagName":"v0.2.0","url":"https://github.com/x812033727/Ti/releases/tag/v0.2.0"}
~~~~

```bash
timeout 60 gh api repos/x812033727/Ti/releases/tags/v0.2.0 --jq '{body,tag_name,html_url,id,created_at,published_at}'
```

~~~~text
{"body":"# Release 0.2.0\n\n## ⚠️ Breaking Changes\n\n\u003e 獨立頂層區塊，彙整所有破壞性變更；各版本節內亦保留對應摘要供版本歷史檢索。\n\n### `TI_REQUIRE_CHOWN` 預設已改為 `strict`（自 `0.2.0` 起）\n\n- **① 行為變動**：state 檔案（history meta/events、`backlog.json`）的安全寫入模式\n  `TI_REQUIRE_CHOWN` **已改為 `strict` 預設**。寫入後會驗證檔案 owner 為 `root`（uid 0）且\n  `nlink=1`，任一不符即整體失敗、不落地半截檔。**只在以 `root` 執行的部署下會直接成功**。\n- **② 原因**：防止 symlink 攻擊與非 root 程序竄改 state，確保僅 root-only（uid 0、nlink=1）\n  路徑能寫入；舊版隱含放行會讓被竄改或半截的 state 檔靜默落地，屬安全強化而非降級。\n- **③ before / after 遷移範例**：以下為非 root 環境的遷移寫法。\n\n  之前（`0.1.x`，未顯式設定即隱含放行）：\n\n  ```bash\n  # 不設定，state 寫入不驗 owner，非 root 也能落地\n  python3 -m studio ...\n  ```\n\n  之後（`0.2.0`，`strict` 已成預設；非 root 須顯式選擇逃生艙）：\n\n  ```bash\n  # 非 root 部署：過渡期放行但記 warning\n  export TI_REQUIRE_CHOWN=warn\n  # 或完全停用 owner 驗證\n  export TI_REQUIRE_CHOWN=off\n  python3 -m studio ...\n  ```\n\n- **④ 生效版本**：自 `0.2.0` 起生效（即本版，非未來版本；`strict` 已是當前預設行為）。\n\n**三態與逃生艙**：`strict`（預設，安全側）／`warn`（過渡，放行但警告）／`off`（停用驗證）。\n若為**非 root** 環境部署，請顯式設定 `TI_REQUIRE_CHOWN=warn`（過渡）或 `off`（停用）作為使用者側逃生艙。\n\n**錯誤值 fail-safe**：無法辨識的值（如打錯字）一律 **fail-safe 回退為 `strict`** 並記 warning，\n不會靜默降級——打錯字不等於關閉驗證。\n\n**遷移指引**：完整說明見 README 的「state 安全寫入（TI_REQUIRE_CHOWN）」小節，\n以及 `.env.example` 內的 `TI_REQUIRE_CHOWN` 範例。\n\n_完整變更記錄見 CHANGELOG.md（v0.2.0）。_","created_at":"2026-06-21T13:15:15Z","html_url":"https://github.com/x812033727/Ti/releases/tag/v0.2.0","id":342528036,"published_at":"2026-06-21T13:15:44Z","tag_name":"v0.2.0"}
~~~~

### #2 線上 body 結構斷言

```bash
timeout 60 python3 scripts/check_release_body_structure.py
```

```text
Traceback (most recent call last):
  File "/opt/ti-autopilot-work.lanes/lane-ap7726860542-2/scripts/check_release_body_structure.py", line 28, in <module>
    from studio.release_note import BREAKING_HEADING, pyproject_version
ModuleNotFoundError: No module named 'studio'
```

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

### #3 release-smoke 觸發

```bash
timeout 60 gh run view 27905531397 --repo x812033727/Ti --json databaseId,event,status,conclusion,headBranch,url,createdAt,updatedAt,workflowName,displayTitle,headSha,number,name,attempt,startedAt
```

```json
{"attempt":1,"conclusion":"success","createdAt":"2026-06-21T13:15:45Z","databaseId":27905531397,"displayTitle":"v0.2.0","event":"release","headBranch":"v0.2.0","headSha":"f7715fa042c37d6d4f04da3f696176fdce9855da","name":"Release smoke","number":2,"startedAt":"2026-06-21T13:15:45Z","status":"completed","updatedAt":"2026-06-21T13:15:55Z","url":"https://github.com/x812033727/Ti/actions/runs/27905531397","workflowName":"Release smoke"}
```

```bash
timeout 60 gh api repos/x812033727/Ti/actions/runs/27905531397 --jq '{id,event,status,conclusion,html_url,created_at,updated_at,head_branch,head_sha,name,path,run_attempt,run_number,workflow_id,display_title}'
```

```json
{"conclusion":"success","created_at":"2026-06-21T13:15:45Z","display_title":"v0.2.0","event":"release","head_branch":"v0.2.0","head_sha":"f7715fa042c37d6d4f04da3f696176fdce9855da","html_url":"https://github.com/x812033727/Ti/actions/runs/27905531397","id":27905531397,"name":"Release smoke","path":".github/workflows/release-smoke.yml","run_attempt":1,"run_number":2,"status":"completed","updated_at":"2026-06-21T13:15:55Z","workflow_id":296211954}
```

```bash
python3 - <<'PY'
import json, subprocess, pathlib, sys
root = pathlib.Path('.')
evidence = json.loads((root / 'docs/evidence/release-smoke-v0.2.0-trigger.json').read_text())
expected = {k: evidence[k] for k in ('run_id','event','status','conclusion')}
cmd1 = ['gh','run','view','27905531397','--repo','x812033727/Ti','--json','databaseId,event,status,conclusion,headBranch,url,createdAt,updatedAt,workflowName,displayTitle,headSha,number,name,attempt,startedAt']
cmd2 = ['gh','api','repos/x812033727/Ti/actions/runs/27905531397','--jq','{id,event,status,conclusion,html_url,created_at,updated_at,head_branch,head_sha,name,path,run_attempt,run_number,workflow_id,display_title}']
run_view = json.loads(subprocess.check_output(cmd1, text=True))
rest = json.loads(subprocess.check_output(cmd2, text=True))
checks = {
    'gh_run_view.run_id': str(run_view['databaseId']) == expected['run_id'],
    'gh_run_view.event': run_view['event'] == expected['event'],
    'gh_run_view.status': run_view['status'] == expected['status'],
    'gh_run_view.conclusion': run_view['conclusion'] == expected['conclusion'],
    'rest_run.id': str(rest['id']) == expected['run_id'],
    'rest_run.event': rest['event'] == expected['event'],
    'rest_run.status': rest['status'] == expected['status'],
    'rest_run.conclusion': rest['conclusion'] == expected['conclusion'],
}
failed = [k for k, v in checks.items() if not v]
print(json.dumps({'expected': expected, 'checks': checks, 'all_match': not failed}, ensure_ascii=False))
sys.exit(0 if not failed else 1)
PY
```

```text
{"expected": {"run_id": "27905531397", "event": "release", "status": "completed", "conclusion": "success"}, "checks": {"gh_run_view.run_id": true, "gh_run_view.event": true, "gh_run_view.status": true, "gh_run_view.conclusion": true, "rest_run.id": true, "rest_run.event": true, "rest_run.status": true, "rest_run.conclusion": true}, "all_match": true}
```

## 結論

三證據已閉環。`#3` 以 `gh run view` 與 REST 實跑重驗，`event/status/conclusion` 與 evidence 一致。  
`gh run view` 沒有直接補 `path` 時，以 `N/A` 標示，並由 REST 補驗。
