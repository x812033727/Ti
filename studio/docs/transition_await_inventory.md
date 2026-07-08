# 過渡段 await 定位清單

目的：盤點並行 lane 從「本波任務全部收斂」到「demo 開始」之間的所有 await，
確認哪些帶 timeout、真無界葉節點實際有幾個。行號由守門測試對現行
`studio/orchestrator.py` 動態重算，本清單為被校驗方（防文件漂移）。

## 邊界

- lane 全收斂 -> demo 開始
- 起點：`studio/orchestrator.py:2355` `await asyncio.gather(` —— 本波所有 lane task 收齊。
- 主要過渡段：`studio/orchestrator.py:2359` `await self._integrate_wave(` —— 進入整合入口。
  （此處直呼現存實碼 `_integrate_wave`；產品碼不因測試而額外包一層 wrapper。）
- 過渡段實作：`studio/orchestrator.py:2532` `async def _integrate_wave(`。
- 終點：`studio/orchestrator.py:1405` `_stage_demo` -> `studio/orchestrator.py:1407` `await self._final_demo()`。

## 無界葉節點結論

過渡段真無界 await 葉節點：**0 個**。

實證：`studio/orchestrator.py` 全檔無 `create_subprocess` / `proc.wait()` / `proc.communicate()`，
所有 subprocess 一律委派 `runner.*`；**過渡段 subprocess 收尾均帶 timeout**（SSOT 落在
`studio/runner.py` 的 `_finalize_proc` / `_wait_proc` / `run_http_demo`，非 orchestrator 重複實作）。
人類插話等待 `studio/orchestrator.py:424` 的 `queue.get()` 也已由 `asyncio.wait_for` 1 秒切片包住，
可被 stop 即時中止。故廢棄 pyc 舊清單「真無界葉節點有兩個（`proc.wait()` / ws send）」的假設。

## await 表格

| 位置 | await | 型別 | 現有 timeout | 判定 |
|---|---|---|---|---|
| studio/orchestrator.py:2355 | await asyncio.gather(...) | lane fan-in（聚合，非葉） | gather 本身無 wait_for；lane leaf 由 runner/TURN timeout 包住 | 有界 |
| studio/orchestrator.py:2359 | await self._integrate_wave(...) | 過渡段整合入口 | 內部 git leaf 走 runner timeout | 有界 |
| studio/orchestrator.py:2532 | async def _integrate_wave(...) | 過渡段整合實作 | 逐一 merge/teardown 的 git leaf 走 runner timeout | 有界 |
| studio/orchestrator.py:424 | await asyncio.wait_for(self._intervention.get(), timeout=min(1.0, remaining)) | queue.get()（澄清階段，非過渡段） | wait_for + 1 秒切片 timeout | 有界 |
| studio/orchestrator.py:1407 | await self._final_demo() | demo 入口（終點） | subprocess leaf 走 runner timeout | 有界 |

## 結論

過渡段 `await asyncio.gather`（`:2355`）收齊 lane 後，經 `await self._integrate_wave`（`:2359`）
逐步 merge / teardown / fallback，最後到 `_stage_demo`（`:1405`）進 demo。全段 subprocess 皆委派
`runner.*` 並帶 timeout，`queue.get()` 有 `wait_for`；過渡段真無界 await 葉節點數量為 **0**。
