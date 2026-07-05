# Release E2E 移交待辦：`v*` tag-push 生產鏈半閉環聲明

> 本文件是任務 #4 的**移交明文**。目的只有一個：把「哪些已被測試閉環、哪些仍是半閉環」
> 講清楚，並列出**具名人工核對步驟**，避免後人把單元／守護測試綠燈誤當成真實生產 E2E
> 已通過。本文件為 additive／可逆，不修改任何護欄本體。

## 半閉環聲明（最重要，先讀）

**真實 `v*` tag-push 端到端（E2E）鏈中，v0.2.0 此鏈已生產閉環；後續版本仍半閉環、尚待逐版
生產驗證。** 其中 `release: published → release-smoke` 與 GitHub release 頁實際 body 頂部
Breaking 置頂，皆已由 v0.2.0 線上證據封口（見下方邊界表與對應 evidence）；目前多數單元／
守護測試只證明「推 tag 前的 render 結構正確」，**不代表** GitHub 生產環境已對每一版都
實跑過完整鏈
`push tag → gh release create → release: published → release-smoke`。除已具生產證據的環節外，
第一次正式打新 `v*` tag 發 release 後，仍須依下方「發佈後人工核對步驟」逐項確認，才算真正閉環。

換句話說：**未具生產證據的環節，綠燈 ≠ 已在生產跑過**。不得以測試綠冒充端到端。

## 已閉環 vs 未閉環邊界

| 環節 | 狀態 | 依據（具名勾稽） |
|---|---|---|
| 推 tag「前」兩出口皆帶 `## ⚠️ Breaking Changes` heading + 四要素 | ✅ 已閉環（守護測試） | `tests/autopilot/test_qa_task4_pretag_breaking_outlets.py::test_pretag_outlet_carries_block`（`OUTLETS` 兩出口 parametrize） |
| ④ 生效版本逐字對應 pyproject 當前版本（0.2.0），非任意版本假綠 | ✅ 已閉環（守護測試） | `test_qa_task4_pretag_breaking_outlets.py::test_pretag_effective_version_matches_pyproject_in_both_outlets`；尺規 `tests/autopilot/_release_check.py::version_matches_effective` |
| 缺區塊／缺任一要素／版本改舊版 → 兩出口翻紅（黑樣本成對自證） | ✅ 已閉環（守護測試） | `test_qa_task4_pretag_breaking_outlets.py` 的 `test_black_sample_missing_block_pairs_red`、`test_black_sample_missing_each_element_pairs_red`、`test_black_sample_stale_effective_version_pairs_red` |
| 發佈文件契約（GH_PAT 四項／DoD／半閉環聲明／403 runbook）在 `CLAUDE.md` | ✅ 已閉環（守護測試） | `tests/autopilot/test_qa_task4_release_docs_dod.py`（全數綠，含黑樣本 mutation 自證判別力） |
| CI `test` job 於每次 push 跑 `pytest`，在 `v*` tag 推出前即攔截 | ✅ 已閉環 | `.github/workflows/ci.yml` 的 `test` job；閘門在 push 觸發，早於任何 `v*` tag-push |
| **真實 `v*` tag-push → GitHub release 頁實際 body 頂部 Breaking 置頂** | ✅ 已生產閉環（實跑核對） | `docs/evidence/release-v0.2.0-online-body.json`（`body_match=true`、`body_sha256`）＋`docs/evidence/release-v0.2.0-body-structure-verdict.json`（`verdict=PASS`、`頂部即 Breaking 置頂=true`、`雙來源正規化後逐字相等=true`）＋`scripts/check_release_body_structure.py`＋`tests/autopilot/test_qa_body_pinning_evidence.py::test_handoff_body_row_is_green_with_evidence_paths` |
| **`release: published` 事件實際觸發 `release-smoke.yml`** | ✅ **已生產閉環（實跑核對）** | 生產證據 `docs/evidence/release-smoke-v0.2.0-trigger.json`：run [27905531397](https://github.com/x812033727/Ti/actions/runs/27905531397)，`event=release`／`status=completed`／`conclusion=success`（`gh run view`＋REST 雙路核對一致）；觸發契約結構另由 `test_qa_task4_release_docs_dod.py::test_task4_commit_does_not_alter_release_smoke_trigger` 守護 |

## 發佈後人工核對步驟（具名、可逐項打勾）

第一次正式打 `v*` tag 後，**必做**下列步驟；任一項不符即視為 E2E 未通過，先停止後續發佈動作並修正 CHANGELOG／設定。

### A. 發佈前（推 tag 前）先本機核對 body.md
1. 跑 `python3 scripts/publish_release.py` 產出 `body.md`（版本走 `studio.release_note.pyproject_version()`，本輪為 `0.2.0`）。
2. 開 `body.md`，確認**最上方第一個頂層 `## ` 區塊就是 `## ⚠️ Breaking Changes`**（不得被其他章節擠到後面）。
3. 確認該區塊內含四要素：① 行為變動、② 原因、③ before/after 遷移範例、④ 生效版本（須寫 `自 0.2.0 起`，與 pyproject 同源）。
4. 確認仍保有逃生艙字串 `TI_REQUIRE_CHOWN=warn/off`（`warn`/`off` 即刻生效，非 deprecation 過渡）。

### B. 發佈後（推 tag、release 建立後）在 GitHub 上核對 —— **本輪新增、閉環關鍵**
5. `gh release view "$TAG" --json body --jq '.body'`（或直接開 GitHub release 頁），核對**線上 release body 與本機 `body.md` 逐字一致**。
6. 確認線上 body **頂部第一個頂層區塊即 `## ⚠️ Breaking Changes`**，四要素與 `TI_REQUIRE_CHOWN=warn/off` 逃生艙字串都在。
7. 到 repo **Actions** 分頁，確認 `release: published` 已**實際觸發** `release-smoke.yml` 且該 run 綠燈（若沒被觸發，優先檢查建立 release 的 token 是否仍是 `secrets.GH_PAT`——用內建 `GITHUB_TOKEN` 建 release 會被 GitHub 防遞迴機制吞掉 `release: published`）。
8. 上述任一項不符：**先停發**，回 CHANGELOG／設定修正後重跑 A、B，不得帶病發佈。

## 離線實測佐證（本輪已跑，非生產 E2E）

依設計決策，離線核對走底層 `render_release_body()` 寫 `$TMPDIR`，**不落 repo root**（避免 pre-commit 綠／CI 紅分歧）。本輪實跑結果：

- `版本: 0.2.0`；body 頂部第一個頂層區塊 `-> ## ⚠️ Breaking Changes`；含逃生艙 `TI_REQUIRE_CHOWN=warn/off`。
- repo root 無 `body.md` 殘留（`git status` 確認）。

實際 body 頂部片段（節錄）：

```
# Release 0.2.0

## ⚠️ Breaking Changes

### `TI_REQUIRE_CHOWN` 預設已改為 `strict`（自 `0.2.0` 起）

- **① 行為變動**：...  TI_REQUIRE_CHOWN 已改為 strict 預設 ...
- **② 原因**：防止 symlink 攻擊與非 root 程序竄改 state ...
- **③ before / after 遷移範例**：...
```

> ⚠️ 此為**離線 render** 結果，只證明「推 tag 前結構正確」。**真實 `v*` tag-push 生產 E2E 仍待第一次正式發 release 後由步驟 B 人工閉環。**

## 邊界與不做事項

- 本文件不修改 `BREAKING_HEADING` 常數，也不硬寫版本字面值；查出漂移一律改 `CHANGELOG.md`，禁止反向改常數。
- 版本權威唯一來源為 `studio.release_note.pyproject_version()`。
- 本輪不補 `--verify-tag`、不鎖 actions commit SHA——理由見 `docs/release-pipeline-requirements-audit.md` 與 `CLAUDE.md`「發佈鏈 DoD」。
