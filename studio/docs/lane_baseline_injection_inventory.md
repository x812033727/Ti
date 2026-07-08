# Lane baseline 注入現況稽核

目的：供 ARCHITECTURE.md「baseline 注入契約」決策表對齊。這份清單描述目前實作，不等同已落地的 lane baseline env/manifest 契約。

## 結論

- 引用以函式/類別 marker 為主，行號不是契約來源，避免純文件稽核隨程式碼移動而漂移。
- `LaneContext` 現況只保存隔離執行狀態：`lane_id`、`cwd`、`experts`、`critics`、`branch`、`last_commit`、`notes_buffer`（`studio/orchestrator.py`，marker: `class LaneContext`）。
- `_open_lane()` 實際注入的是 worktree 路徑、branch 名稱、base commit、獨立 expert session suffix；沒有 lane 專屬 env，也沒有寫入 manifest（`studio/orchestrator.py`，marker: `async def _open_lane`）。
- `_integrate_wave()` 只做序列化合併、notes flush、teardown 與降級重跑；沒有新增 env/manifest 注入（`studio/orchestrator.py`，marker: `async def _integrate_wave`）。
- 目前所有 `TI_*` 都是 process-level config 讀取，不是 per-lane baseline；lane 子程序大多繼承父程序 env。

## 逐項對照

| 項目 | 實際來源與欄位 | env 注入 | manifest 欄位 | 缺失/失敗現況 |
|---|---|---|---|---|
| 主 lane context | `LaneContext("main", self.cwd, experts, self._critics, last_commit=self._last_commit)`（`StudioSession._run`） | 無 lane 專屬 env | 無 | 無 cwd 時並行關閉，走循序/測試路徑 |
| 並行開關 | `config.PARALLEL_TASKS_ENABLED` + `bool(self.cwd)` 決定是否開 lane（`StudioSession._run_waves`） | 由 process env `TI_PARALLEL_TASKS` 讀入 config；非 lane 注入 | 無 | 關閉或無 cwd 時退回單主 lane |
| lane 切分數 | `_plan_lanes()` 依 `PARALLEL_LANES`、`LLM_MAX_CONCURRENCY`、wave 大小切分 | process env `TI_PARALLEL_LANES`、`TI_LLM_MAX_CONCURRENCY` 讀入 config；非 lane 注入 | 無 | 最少 1 條 lane，等同循序 |
| branch 名稱 | `lane-{session_id}-{task_ids}`（`StudioSession._open_lane`） | 無 | 無 | branch 名稱交給 runner 驗證；失敗回 None |
| worktree 路徑 | `<cwd>.lanes/<safe branch>`（`StudioSession._lane_worktree_path`） | 無 | 無 | 建立失敗時該 lane tasks 進 deferred |
| base commit | `self._last_commit or "HEAD"` 傳給 `git_worktree_add`（`StudioSession._open_lane`） | 無 | 無 | 初始 commit 失敗會讓 worktree 開不起來，轉 deferred |
| git worktree 啟動 | `git worktree add -b <branch> <path> <base>`（`runner.git_worktree_add`） | `run_command_exec(..., sandbox=False)` 未傳 `env`，即繼承父 env | 無 | `git_worktree_add()` 回 False，orchestrator 廣播「並行降級」後主幹重跑 |
| lane context 建立 | `LaneContext(branch, wt, {}, branch=branch)`（`StudioSession._open_lane`） | 無 | 無 | `last_commit` 預設 None；無 baseline manifest 可補值 |
| lane experts | `factory(role, f"{session_id}:{suffix}", cwd)` 鏡射主 experts（`StudioSession._build_lane_experts`） | 無 lane 專屬 env；只用建構參數傳 session suffix 與 cwd | 無 | factory 例外未在 `_open_lane()` 內轉降級，會往外拋；這不是 baseline 行為 |
| Claude expert | `ClaudeAgentOptions(..., cwd=str(cwd), model=..., sandbox=..., hooks=...)`（`experts._build_client`） | 未在本層設定 env | 無 | cwd 外寫入由 PreToolUse hook 擋；非 baseline manifest |
| Codex expert | 子程序 `cwd=str(self.cwd)`，`env=_codex_env()`（`providers.CodexExpert._run_codex`） | `_codex_env()` 複製父 env；只有 `CODEX_HOME` 有值時額外設定（`providers._codex_env`） | 無 | `CODEX_HOME` 是 provider 全域設定，非 per-lane baseline |
| Antigravity expert | 子程序 `cwd=str(self.cwd)`，`env=os.environ.copy()`（`providers.AntigravityExpert._run_antigravity`） | 繼承父 env，無 lane 專屬 key | 無 | 無 provider 層 baseline manifest |
| runner 自測/指令 | `run_command_exec()` 的 `env=None` 時繼承父 env；只有呼叫端明傳 env 才合併 | lane 路徑沒有明傳 env | 無 | 沙箱缺失會 fail-closed，但那是 runner 安全策略，不是 lane baseline |
| `_integrate_wave()` 合併 | 依 `lane_id` 排序合併、flush notes、teardown（`StudioSession._integrate_wave`） | 無 | 無 | lane crash、worktree deferred、merge conflict 都降級到主幹序列化重跑 |
| notes 緩衝 | `notes_buffer` 波末 flush 到共享 `NOTES.md`（`StudioSession._flush_lane_notes`） | 無 | 無 | crash lane 會清掉 notes，避免不可信成果污染共享筆記 |
| teardown | `git worktree remove --force` + best-effort delete branch（`runner.git_worktree_remove`） | 無 | 無 | best-effort；run finally 另有兜底清理 |

## 非 lane 注入但會被決策表引用的 repo 實例

| 實例 | 現況語意 | 用途 |
|---|---|---|
| `AUTOPILOT_REPO` | autopilot push guard 缺失或目標不符時 `return (False, reason)`，屬 fail-closed 類比（`DECISIONS.md`，marker: ``Guard 條件二選一觸發``）。 | 決策表安全/正確性關鍵項的 closed 佐證；不是 lane baseline 現有欄位。 |
| `TI_DISCUSS_MODE` | config 白名單非法值 fallback `legacy`，屬 fail-open 類比（`DECISIONS.md`，marker: ``DISCUSS_MODE = os.getenv("TI_DISCUSS_MODE", "legacy")``）。 | 決策表非關鍵增益項的 open 佐證；不是 lane baseline 現有欄位。 |

## 給決策表的對齊建議

- 現況可描述為「顯式建構參數 + process-level config + env 繼承」，沒有 `lane manifest` 層。
- 若文件要寫 `顯式注入 > env(TI_*) > lane manifest > 模組 DEFAULT`，需標註「前瞻契約」：目前只具備顯式建構參數與 env/config 兩層。
- 現有 lane 失敗策略偏 fail-open/degrade：worktree 開失敗、lane crash、merge conflict 都回主幹序列化重跑；這可作為「並行最佳化缺失不阻斷交付」的 repo 實例。
- 尚無守門測試覆蓋 lane baseline env/manifest 欄位，因為該注入層尚未存在；落地後需補測試鎖定欄位、優先序與 fail-open/closed 策略。
