# 過渡段 await 定位清單

目的：盤點並行 lane 從「本波任務全部收斂」到「demo 開始」之間的所有 await，
逐行列出行號、型別與 timeout 狀態，確認真無界葉節點實際有幾個。行號由守門測試對現行
`studio/orchestrator.py` 動態重算，本清單為被校驗方（防文件漂移）。

## 邊界

- lane 全收斂 -> demo 開始
- 起點：`studio/orchestrator.py:2371` `await asyncio.gather(` —— 本波所有 lane task 收齊。
- 主要過渡段：`studio/orchestrator.py:2375` `await self._integrate_wave(` —— 進入整合入口。
  （此處直呼現存實碼 `_integrate_wave`；產品碼不因測試而額外包一層 wrapper。）
- 過渡段實作：`studio/orchestrator.py:2548` `async def _integrate_wave(`。
- 終點：`studio/orchestrator.py:1421` `_stage_demo` -> `studio/orchestrator.py:1423` `await self._final_demo()`。

## 無界葉節點結論

過渡段真無界 **subprocess** 葉節點：**0 個**（此數僅限 subprocess 維度）。

實證：`studio/orchestrator.py` 全檔無 `create_subprocess` / `proc.wait()` / `proc.communicate()`，
所有 subprocess 一律委派 `runner.*`；**過渡段 subprocess 收尾均帶 timeout**（SSOT 落在
`studio/runner.py` 的 `_finalize_proc` / `_wait_proc` / `run_http_demo`，非 orchestrator 重複實作）。
人類插話等待 `studio/orchestrator.py:429` 的 `queue.get()` 也由 `asyncio.wait_for` 1 秒切片包住，
可被 stop 即時中止。

但**網路 await 並非全部有界**：過渡段 6 個 `await self.broadcast(`（行 2553 / 2605 / 2642 /
2652 / 2685 / 2723）最終委派 `studio/ws.py:355` 的 `await websocket.send_json(d)`，
**無 `wait_for`、無應用層 timeout**——backpressure 或 stalled client 下此 await 會阻塞。
故如實標為「未設應用層 timeout 的網路 await」，不標「即時返回／有界」。
就舊清單「真無界葉節點有兩個（`proc.wait()` / ws send）」而言：`proc.wait()` 一項已全委派
`runner.*`（帶 timeout）故消除；ws send 一項經實證仍為無本地 timeout 的網路 await，予以保留如實記錄。

## await 表格（逐行列出過渡段鏈全部 await）

下表逐行覆蓋起點 `gather`、整合入口 `_integrate_wave` 及其 merge / teardown / 解衝突 / 序列化重跑
子鏈（`_merge_lane` / `_serialize_lane_rerun` / `_resolve_conflict_in_lane` / `_merge_resolved_lane_back`）、
終點 `_final_demo`，以及澄清段的 `queue.get()`。共 36 列。

| 位置 | await | 型別 | 現有 timeout | 判定 |
|---|---|---|---|---|
| studio/orchestrator.py:429 | await asyncio.wait_for( | queue.get()（澄清段，非過渡段） | wait_for + 1 秒切片 | 有界 |
| studio/orchestrator.py:1423 | await self._final_demo() | demo 入口（終點） | subprocess leaf 走 runner timeout | 有界 |
| studio/orchestrator.py:2371 | await asyncio.gather( | lane fan-in（聚合） | lane leaf 走 runner/TURN timeout | 有界 |
| studio/orchestrator.py:2375 | await self._integrate_wave(opened, results, deferred, plan_ctx) | 過渡段整合入口 | 內部 git/LLM leaf 帶 timeout | 有界 |
| studio/orchestrator.py:2569 | await self.broadcast( | 事件廣播 | 無 wait_for（委派 ws.py:355 send_json） | 無界網路 await |
| studio/orchestrator.py:2579 | await self._teardown_lane(ctx) | lane 收尾（委派） | 內部 git leaf 走 runner timeout | 有界 |
| studio/orchestrator.py:2588 | await self._merge_lane(lr, plan_ctx) | lane 合併（委派） | 內部 git leaf 走 runner timeout | 有界 |
| studio/orchestrator.py:2590 | await self._teardown_lane(lr.ctx) | lane 收尾（委派） | 內部 git leaf 走 runner timeout | 有界 |
| studio/orchestrator.py:2595 | await self._run_task_in_lane(self._main_ctx, task, plan_ctx) | 任務執行（委派） | LLM/subprocess leaf 走 TURN/runner timeout | 有界 |
| studio/orchestrator.py:2607 | await self._lane_git_snapshot("pre-merge", lr.ctx.branch) | git 快照（委派 runner） | runner _finalize_proc timeout | 有界 |
| studio/orchestrator.py:2608 | await runner.git_merge_worktree(self.cwd, lr.ctx.branch) | git subprocess（委派 runner） | runner _finalize_proc timeout | 有界 |
| studio/orchestrator.py:2618 | await runner.git_head_short(self.cwd) | git subprocess（委派 runner） | runner _finalize_proc timeout | 有界 |
| studio/orchestrator.py:2621 | await self.broadcast( | 事件廣播 | 無 wait_for（委派 ws.py:355 send_json） | 無界網路 await |
| studio/orchestrator.py:2624 | await self._lane_git_snapshot("post-merge-ok", lr.ctx.branch) | git 快照（委派 runner） | runner _finalize_proc timeout | 有界 |
| studio/orchestrator.py:2630 | await runner.git_merge_abort(self.cwd) | git subprocess（委派 runner） | runner _finalize_proc timeout | 有界 |
| studio/orchestrator.py:2632 | await self._resolve_conflict_in_lane(lr, plan_ctx) | 解衝突（委派） | git/LLM leaf 帶 timeout | 有界 |
| studio/orchestrator.py:2637 | await self._serialize_lane_rerun( | 序列化重跑（委派） | 任務 leaf 走 TURN timeout | 有界 |
| studio/orchestrator.py:2650 | await self._serialize_lane_rerun( | 序列化重跑（委派） | 任務 leaf 走 TURN timeout | 有界 |
| studio/orchestrator.py:2658 | await self.broadcast( | 事件廣播 | 無 wait_for（委派 ws.py:355 send_json） | 無界網路 await |
| studio/orchestrator.py:2668 | await self.broadcast(events.phase_change(self.session_id, "合併衝突", reason)) | 事件廣播 | 無 wait_for（委派 ws.py:355 send_json） | 無界網路 await |
| studio/orchestrator.py:2677 | await self._run_task_in_lane(self._main_ctx, task, plan_ctx) | 任務執行（委派） | LLM/subprocess leaf 走 TURN/runner timeout | 有界 |
| studio/orchestrator.py:2693 | await runner.git_merge_ref_into(lr.ctx.cwd, self._last_commit) | git subprocess（委派 runner） | runner _finalize_proc timeout | 有界 |
| studio/orchestrator.py:2696 | await runner.git_commit(lr.ctx.cwd, f"併入主幹 {self._last_commit}") | git subprocess（委派 runner） | runner _finalize_proc timeout | 有界 |
| studio/orchestrator.py:2698 | await self._merge_resolved_lane_back(lr) | 解衝突後合回（委派） | git leaf 走 runner timeout | 有界 |
| studio/orchestrator.py:2701 | await self.broadcast( | 事件廣播 | 無 wait_for（委派 ws.py:355 send_json） | 無界網路 await |
| studio/orchestrator.py:2709 | await self._speak( | LLM 對話 | provider/TURN timeout | 有界 |
| studio/orchestrator.py:2718 | await runner.git_merge_abort(lr.ctx.cwd) | git subprocess（委派 runner） | runner _finalize_proc timeout | 有界 |
| studio/orchestrator.py:2721 | await runner.git_conflict_markers_present(lr.ctx.cwd) | git subprocess（委派 runner） | runner _finalize_proc timeout | 有界 |
| studio/orchestrator.py:2722 | await runner.git_merge_abort(lr.ctx.cwd) | git subprocess（委派 runner） | runner _finalize_proc timeout | 有界 |
| studio/orchestrator.py:2725 | await runner.git_commit(lr.ctx.cwd, f"化解與主幹 {self._last_commit} 的合併衝突") | git subprocess（委派 runner） | runner _finalize_proc timeout | 有界 |
| studio/orchestrator.py:2726 | await runner.git_merge_abort(lr.ctx.cwd) | git subprocess（委派 runner） | runner _finalize_proc timeout | 有界 |
| studio/orchestrator.py:2728 | await self._merge_resolved_lane_back(lr) | 解衝突後合回（委派） | git leaf 走 runner timeout | 有界 |
| studio/orchestrator.py:2732 | await runner.git_merge_worktree(self.cwd, lr.ctx.branch) | git subprocess（委派 runner） | runner _finalize_proc timeout | 有界 |
| studio/orchestrator.py:2734 | await runner.git_merge_abort(self.cwd) | git subprocess（委派 runner） | runner _finalize_proc timeout | 有界 |
| studio/orchestrator.py:2736 | await runner.git_head_short(self.cwd) | git subprocess（委派 runner） | runner _finalize_proc timeout | 有界 |
| studio/orchestrator.py:2739 | await self.broadcast( | 事件廣播 | 無 wait_for（委派 ws.py:355 send_json） | 無界網路 await |

## 結論

過渡段 `await asyncio.gather`（`:2355`）收齊 lane 後，經 `await self._integrate_wave`（`:2359`）
逐行 merge / teardown / 解衝突 / fallback（上表 36 個 await 全數列出），最後到 `_stage_demo`
（`:1405`）進 demo。全段 subprocess 皆委派 `runner.*` 並帶 timeout，`queue.get()` 有 `wait_for`；
真無界 subprocess 葉節點數量為 **0**。唯 6 個 broadcast await 最終走 `ws.py:355` `send_json`，
為無本地 `wait_for` 的網路 await，已於上表與結論如實標記，不計入「有界」。
