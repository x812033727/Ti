# Metrics 面板截圖驗證

## 結論

完成。`scripts/capture_metrics.py` 以 `TI_OFFLINE=1` 啟動本機 server，透過既有
`chromedriver` 的 W3C HTTP/JSON 介面開啟 metrics 面板，完成 DOM 斷言並輸出 PNG。

## 執行指令

```bash
TI_OFFLINE=1 .venv/bin/python scripts/capture_metrics.py
```

結果：exit code 0。

關鍵輸出：

```text
PASS seed: metrics-panel-fixture-... scorecard tasks_total=1
PASS health: http://127.0.0.1:8799/api/health
PASS assert: #metricsPanel visible (hidden=False)
PASS assert: #metricsBody contains "活躍場次" (found)
PASS assert: #metricsBody contains "記分卡" (found)
PASS js-error: no JavaScript exception in browser log
WARN screenshot: docs/screenshots/metrics_panel.png exists; pass --overwrite to replace
PASS cleanup: stopped chromedriver
PASS cleanup: stopped server
```

瀏覽器 log 有 1 筆 `SEVERE`，內容是 `/favicon.ico` 404；不是 JavaScript exception。

## 截圖

截圖路徑：`docs/screenshots/metrics_panel.png`

PNG 檢查：

```text
magic: 89504e470d0a1a0a
size: 169811 bytes
```

眼見觀察：右側「運維指標」抽屜已開啟，可見「活躍場次」、「歷史場次」、
「記分卡（場次）」、「任務」、「測試通過率」、「Demo 通過率」等欄位；
記分卡區塊顯示 1 場、成功率 100%，代表 `scorecard.n > 0` 已渲染。

## 假綠排除

反向指令：

```bash
.venv/bin/python scripts/capture_metrics.py --skip-open-panel --overwrite
```

結果：exit code 1。

關鍵輸出：

```text
FAIL assert: #metricsPanel visible (hidden=True)
FAIL assert: #metricsBody contains "活躍場次" (text='')
FAIL assert: #metricsBody contains "記分卡" (text='')
FAIL capture_metrics: DOM assertions failed
```

結論：停用開面板步驟後腳本會轉紅，DOM 斷言具判別力。

## 產物策略

`docs/screenshots/metrics_panel.png` 進 repo，作為本輪手動驗收截圖。
腳本預設不覆寫既有 PNG，但仍會完成 server/WebDriver/DOM 驗證並 exit 0；
需要重產截圖時必須明確加 `--overwrite`，避免二進位 churn。

## 回歸檢查

```text
.venv/bin/python -m ruff check .                                  -> All checks passed
.venv/bin/python -m pytest tests/server/test_metrics_endpoint.py \
  tests/server/test_scorecard.py tests/server/test_server_smoke.py -q -> 16 passed
.venv/bin/python -m py_compile scripts/capture_metrics.py          -> pass
```

全套 `.venv/bin/python -m pytest -q` 以 `timeout 60` 執行，60 秒時仍在 40% 左右，
因此本輪未宣稱全套完成；不是測試失敗。
