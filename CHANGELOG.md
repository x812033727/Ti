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
  python -m studio ...
  ```

  之後（`0.2.0`，`strict` 已成預設；非 root 須顯式選擇逃生艙）：

  ```bash
  # 非 root 部署：過渡期放行但記 warning
  export TI_REQUIRE_CHOWN=warn
  # 或完全停用 owner 驗證
  export TI_REQUIRE_CHOWN=off
  python -m studio ...
  ```

- **④ 生效版本**：自 `0.2.0` 起生效（即本版，非未來版本；`strict` 已是當前預設行為）。

**三態與逃生艙**：`strict`（預設，安全側）／`warn`（過渡，放行但警告）／`off`（停用驗證）。
若為**非 root** 環境部署，請顯式設定 `TI_REQUIRE_CHOWN=warn`（過渡）或 `off`（停用）作為使用者側逃生艙。

**錯誤值 fail-safe**：無法辨識的值（如打錯字）一律 **fail-safe 回退為 `strict`** 並記 warning，
不會靜默降級——打錯字不等於關閉驗證。

**遷移指引**：完整說明見 README 的「state 安全寫入（TI_REQUIRE_CHOWN）」小節，
以及 `.env.example` 內的 `TI_REQUIRE_CHOWN` 範例。

## [0.2.0] - 2026-06-15

### ⚠️ Breaking Changes

- `TI_REQUIRE_CHOWN` 預設已改為 `strict`（自 `0.2.0` 起生效）。完整四要素（行為變動／原因／
  before-after 遷移／生效版本）見本檔頂端的 [⚠️ Breaking Changes](#️-breaking-changes) 獨立區塊。

### Changed

- `pyproject.toml` 版本字串由 `0.1.0` 升至 `0.2.0`（breaking change → 0.x 階段以 minor bump 標示）。
- 移除 `studio/__init__.py` 的硬寫 `__version__`，版本字串統一以 `pyproject.toml` 為單一事實來源。
