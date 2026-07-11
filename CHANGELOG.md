# Changelog

本專案所有重要變更記錄於此。格式依循 [Keep a Changelog](https://keepachangelog.com/zh-TW/1.1.0/)，
版本號遵循 [語意化版本](https://semver.org/lang/zh-TW/)。版本字串以 `pyproject.toml` 為**單一事實來源**，
本檔不另行硬寫版本。

<!-- 架構伏筆：未來可接入 semantic-release，自動擷取 commit footer 的 BREAKING CHANGE: 生成本區塊；本次為人工維護。 -->

## ⚠️ Breaking Changes

> 獨立頂層區塊，彙整所有破壞性變更；各版本節內亦保留對應摘要供版本歷史檢索。

### `TI_REQUIRE_CHOWN` 預設已改為 `strict`（自 `0.2.0` 起）

- **① 行為變動**：state 檔案（history meta/events、`backlog.json`）的安全寫入模式
  `TI_REQUIRE_CHOWN` **已改為 `strict` 預設**。寫入後會驗證檔案 owner 為 `root`（uid 0）且
  `nlink=1`，任一不符即整體失敗、不落地半截檔。**只在以 `root` 執行的部署下會直接成功**。
- **② 原因**：防止 symlink 攻擊與非 root 程序竄改 state，確保僅 root-only（uid 0、nlink=1）
  路徑能寫入；舊版隱含放行會讓被竄改或半截的 state 檔靜默落地，屬安全強化而非降級。
- **③ before / after 遷移範例**：以下為非 root 環境的遷移寫法。

  之前（`0.1.x`，未顯式設定即隱含放行）：

  ```bash
  # 不設定，state 寫入不驗 owner，非 root 也能落地
  python3 -m studio ...
  ```

  之後（`0.2.0`，`strict` 已成預設；非 root 須顯式選擇逃生艙）：

  ```bash
  # 非 root 部署：過渡期放行但記 warning
  export TI_REQUIRE_CHOWN=warn
  # 或完全停用 owner 驗證
  export TI_REQUIRE_CHOWN=off
  python3 -m studio ...
  ```

- **④ 生效版本**：自 `0.2.0` 起生效（即本版，非未來版本；`strict` 已是當前預設行為）。

**三態與逃生艙**：`strict`（預設，安全側）／`warn`（過渡，放行但警告）／`off`（停用驗證）。
若為**非 root** 環境部署，請顯式設定 `TI_REQUIRE_CHOWN=warn`（過渡）或 `off`（停用）作為使用者側逃生艙。

**錯誤值 fail-safe**：無法辨識的值（如打錯字）一律 **fail-safe 回退為 `strict`** 並記 warning，
不會靜默降級——打錯字不等於關閉驗證。

**遷移指引**：完整說明見 README 的「state 安全寫入（TI_REQUIRE_CHOWN）」小節，
以及 `.env.example` 內的 `TI_REQUIRE_CHOWN` 範例。

## [Unreleased]

### Added

- 監控儀表板成為預設首頁（監控視圖）：Autopilot 狀態英雄列（狀態球/心跳/派工/PR 預算
  ＋暫停恢復/分診/開新討論）、任務統計磁貼（完成率與五態計數）、近 30 天結果堆疊長條、
  provider 額度 meter、績效榜、最新動態；30 秒自動更新（背景分頁暫停輪詢）。
  header 新增「監控／工作室」視圖切換（手機由底部分頁的「監控」分頁接手），開始討論
  或重播歷史時自動切回工作室視圖。資料全部復用既有唯讀端點，零後端變更
  （`web/js/panels/dashboard.js`、`web/css/dashboard.css`）。

- 前端「👥 團隊」面板：角色管理（內建/覆蓋/自建，含反空殼 persona 前端先驗）與
  討論小組管理 UI，首次接上後端既有 `/api/roles`、`/api/groups`；啟動列新增「小組」
  下拉，開場 WS payload 帶 `group`。
- 深／淺／跟隨系統三態主題切換（token 雙主題化、localStorage 持久化、防 FOUC）。
- 動態流程「結構化 stage 卡片編輯器」（增刪/排序/角色多選/閘門/巢狀 task_pipeline），
  保留「{} JSON」進階原文模式；textarea 維持單一真相，儲存管線不變。
- 通用表單 modal（原生 `<dialog>`）取代建立專案／目標 repo 的原生 `prompt()`；
  確認對話框 `openConfirmModal`（alertdialog、取消為預設焦點、danger 紅鈕）取代
  全部 8 處原生 `confirm()`（刪 session/專案/流程/角色/小組、清歷史、重新部署、切換帳號），
  訊息文字逐字保留。
- 無障礙：skip-link、drawer dialog 語意＋Esc/焦點管理、tablist 方向鍵導航、
  動態列表真按鈕化、看板/專家卡 aria；新增平板（901–1180px）響應式斷點。

### Changed

- 前端圖示系統：header 工具列/抽屜標題/手機分頁的 emoji 圖示全面換為內嵌 SVG 線性
  icon sprite（`index.html` `<symbol>`＋`<use>`，免建置）；主題鈕改由 `data-mode` 切換
  三顆 SVG（theme.js 不再覆寫 textContent）。drawer 殼底色改高不透明 `--card-bg`，
  修掉玻璃 backdrop-filter 把底層漸層按鈕暈進面板的色斑；洞察趨勢圖語意色改走
  `--good/--bad` token（原 `--ok/--err` 未定義、淺色主題不跟隨）；styles.css 恢復純
  @import 聚合檔，散落規則歸位 drawers/settings。

- `web/app.js`（2107 行單檔）機械拆分為原生 ES modules（`web/js/`：dom/state/ws/
  events-render/theme/panels/*/components/*），`styles.css` 拆為 `web/css/*` 九檔
  ＋@import 聚合；免建置、URL 面不變（`/static/app.js`、`/static/styles.css`）。
- header 工具列分組（主操作/觀測/系統）、command-deck 兩層化、看板欄計數 badge、
  drawer 加寬至 `min(480px, 100vw)`、補品牌 SVG favicon；討論串階段切換改 hairline
  分隔線樣式；平板工具列溢出時可橫向捲動。
- 前端測試載入機制由 `vm.runInContext` 改「掛 globalThis stub → import ES module」；
  新增 import-graph／主題 token 完整性靜態守護（`tests/test_frontend_modules.py`）。

## [0.2.0] - 2026-06-15

### ⚠️ Breaking Changes

- `TI_REQUIRE_CHOWN` 預設已改為 `strict`（自 `0.2.0` 起生效）。完整四要素（行為變動／原因／
  before-after 遷移／生效版本）見本檔頂端的 [⚠️ Breaking Changes](#️-breaking-changes) 獨立區塊。

### Changed

- `pyproject.toml` 版本字串由 `0.1.0` 升至 `0.2.0`（breaking change → 0.x 階段以 minor bump 標示）。
- 移除 `studio/__init__.py` 的硬寫 `__version__`，版本字串統一以 `pyproject.toml` 為單一事實來源。
