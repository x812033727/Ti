# Release pipeline 需求對照清單

範圍：逐項比對 `.github/workflows/publish-release.yml`、`scripts/publish_release.py`、`.github/workflows/release-smoke.yml`。本文件只記錄已達成/缺口，不修改護欄本體。

## 已達成

| 需求 | 判定 | 依據 |
|---|---|---|
| push `v*` tag 時啟動發佈 workflow | 已達成 | `publish-release.yml:15-18` 設定 `on.push.tags: v*`。 |
| 建立 GitHub Release 使用 `gh release create` | 已達成 | `publish-release.yml:84-89` 的 `Create release` step 執行 `gh release create "$TAG" -F body.md`。 |
| Release 建立後為 published，能被 smoke 接住 | 已達成 | `publish-release.yml:77-89` 未帶 `--draft`；註解明確要求 published。 |
| Release body 由檔案注入，不用 shell 拼多行字串 | 已達成 | `publish-release.yml:77-89` 使用 `-F body.md`；`scripts/publish_release.py:63-73` 寫出 `body.md`。 |
| Body 來源為 `render_tag_notes(...)` | 已達成 | `scripts/publish_release.py:34` 匯入 `render_tag_notes`，`scripts/publish_release.py:47-50` 讀 CHANGELOG 後呼叫 `render_tag_notes(text, ver)`。 |
| 版本字串走 `pyproject_version()`，非 YAML 硬寫 | 已達成 | `publish-release.yml:46-55` 由 Python 讀 `pyproject_version()`；`scripts/publish_release.py:47-50` 未傳 version 時同樣走 `pyproject_version()`。 |
| tag 與版本不一致時 fail-fast | 已達成 | `publish-release.yml:57-68` 比對 `github.ref_name` 與 `v{pyproject_version()}`，不符即退出。 |
| render 失敗不會誤用舊 `body.md` | 已達成 | `scripts/publish_release.py:63-69` 先刪舊 `body.md`，render 成功後才寫檔；例外會造成非零退出。 |
| 可選寫入 `$GITHUB_OUTPUT`，且支援多行 body | 已達成 | `scripts/publish_release.py:53-60` 用隨機 delimiter 寫多行值；`scripts/publish_release.py:71-73` 僅在有 `GITHUB_OUTPUT` 時寫入。 |
| 建立 release 使用 PAT，避免 `GITHUB_TOKEN` 觸發死結 | 已達成 | `publish-release.yml:6-14` 說明死結與權限；`publish-release.yml:84-89` 的 `GH_TOKEN` 來自 `secrets.GH_PAT`。 |
| `GH_PAT` 未設時先 fail-fast | 已達成 | `publish-release.yml:36-44` 的 `Verify PAT` step 以 `test -n "$GH_TOKEN"` 攔空值。 |
| 下游 smoke 由 release published 事件觸發 | 已達成 | `release-smoke.yml:6-8` 設 `on.release.types: [published]`。 |
| smoke 讀「實際 release body」，不是重跑本地 render | 已達成 | `release-smoke.yml:39-52` 使用 `gh release view "$TAG" --json body --jq '.body'`，再寫入 output。 |
| smoke 驗證 release body 含非空頂層 Breaking Changes 區塊 | 已達成 | `release-smoke.yml:54-60` 將 body 放入 `BODY`，執行 `python3 -m studio.release_smoke`。 |
| smoke 權限只需讀 release/body | 已達成 | `release-smoke.yml:10-11` 設 `contents: read`。 |
| 避免 tag/ref 直接插入 shell `run:` | 已達成 | `publish-release.yml:81-89` 以 env `TAG` 傳入並在 shell 內 `"$TAG"` 引用。 |
| publish workflow 內建 `GITHUB_TOKEN` 權限下修 | 已達成 | `publish-release.yml:20-21` 設 `contents: read`；建立 release 的寫入權限由 `publish-release.yml:84-89` 的 `secrets.GH_PAT` 提供。 |
| `GH_PAT` 正式設定/輪替文件 | 已達成 | `CLAUDE.md:171-175` 與 `README.md:217-225` 已固定 Fine-grained PAT、本 repo only、`Contents: Read and write`、secret 名稱 `GH_PAT`，並寫明過期/撤銷 403 時更新同名 secret、不得改 workflow token 路由；`tests/autopilot/test_qa_task4_release_docs_dod.py` 守護。 |
| 半閉環聲明文件化 | 已達成 | `CLAUDE.md:179-182`、`README.md:237-238`、`docs/release-e2e-handoff.md:7-12` 明文標註單元/守護測試為半閉環，後續 `v*` tag-push 仍須逐版生產驗證；`tests/autopilot/test_qa_task4_release_docs_dod.py` 守護。 |
| `v0.2.0` release body 與 release-smoke 生產證據 | 已達成 | `docs/release-e2e-handoff.md:25-26` 勾稽 `docs/evidence/release-v0.2.0-online-body.json`、`docs/evidence/release-v0.2.0-body-structure-verdict.json`、`docs/evidence/release-smoke-v0.2.0-trigger.json`，證明該版線上 body 與 `release: published` smoke 已閉環。 |

## 缺口/待辦

| 項目 | 判定 | 具體描述 |
|---|---|---|
| 後續版本真實 `v*` tag-push 端到端 | 缺口 | `v0.2.0` 已有生產證據封口；但每個新版本仍要逐版確認 `push tag -> gh release create -> release:published -> release-smoke`，不得把上一版證據或單元/守護測試綠燈外推成未來版本 E2E 已通過。正式發 release 時照下方人工清單核對。 |
| PAT 過期/撤銷 fail-fast | 缺口 | `publish-release.yml:36-44` 只檢查 secret 非空；`publish-release.yml:36-39` 已註明過期 PAT 仍會到 Create release 才以 403 失敗。這是運維缺口，不是目前程式碼功能缺口。 |
| Actions 未以 commit SHA 鎖版 | 非阻塞待辦 | `publish-release.yml:29`、`publish-release.yml:32`、`release-smoke.yml:16`、`release-smoke.yml:19` 使用 `actions/checkout@v4` / `actions/setup-python@v5` 可移動標籤；本輪不補，因為不影響 release 驗收鏈，留待專門供應鏈硬化任務處理。 |
| `--verify-tag` | 決策：不補 | `publish-release.yml:89` 目前未加 `--verify-tag`。在現行 `on.push.tags: v*` 與 `Assert tag matches version` 下，tag 已存在且版本有 fail-fast；本輪不需作為驗收必要硬化。若未來新增 `workflow_dispatch` 手動發佈，再重審。 |

### 發 release 時人工確認清單

1. 先跑 `python3 scripts/publish_release.py` 產出 `body.md`。
2. 開 `body.md`，確認最上方就是 `## ⚠️ Breaking Changes`。
3. 確認該區塊內仍有四要素與 `TI_REQUIRE_CHOWN=warn/off` 逃生艙。
4. 再做真實 `v*` tag-push 或 `gh release create "$TAG" -F body.md`，若 body 不對先停發。

結論：三個核心檔的發佈鏈功能、`GH_PAT` 操作文件、半閉環聲明與 `v0.2.0` 生產證據皆已收斂；任務 #3 最小硬化只實作 `GITHUB_TOKEN` 權限下修，`--verify-tag` 與 actions commit SHA 鎖版明確不補並記錄理由。殘留缺口集中在後續版本逐版生產端到端驗證與 PAT 過期無法提前 fail-fast，不是 `gh release create`、body 注入、操作文件或 `release-smoke` 觸發鏈的實作缺失。
