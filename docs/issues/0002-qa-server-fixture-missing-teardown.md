# Bug Issue #0002：QA 測試 server fixture 缺 teardown，server 進程殘留造成測試間污染

- **狀態**：Open
- **優先級**：High（完整測試套件無法乾淨綠燈，掩蓋真實回歸風險）
- **類型**：Bug / Test Infrastructure
- **建立日期**：2026-06-09
- **發現脈絡**：uvicorn 版本鎖定任務（#4）回歸驗證時，完整套件出現 8 個失敗，分流後確認與版本變更無關，根因為本 issue。
- **相關檔案**：
  - `tests/test_qa_task1_server_boot.py`
  - `tests/test_qa_task4_persistence.py`
  - `tests/test_qa_task6_offline_demo.py`
  - （連帶）`tests/test_qa_task5_reread_settings.py`、`tests/test_offline_e2e.py`

---

## 症狀

- **單獨跑** `pytest tests/test_qa_task1_server_boot.py` → **7 passed（全綠）**。
- **完整套件** `pytest tests/` → **8 failed**，且失敗**全部集中在 `test_qa_task1_server_boot.py`**：
  - `test_root_page_served`
  - `test_health_or_root_ok`
  - `test_csp_header_present`
  - `test_security_headers_present`
  - `test_settings_page_reachable`
  - `test_offline_history_recorded`
- 失敗的實際錯誤：readiness 探針 `_probe(...)` 全數回傳 `None`，即 **server 根本沒起來 / 無法連線**（不是 HTTP 4xx/5xx，而是連不上）。

「單獨綠、合跑紅」是測試間污染的典型特徵。

---

## 根因

多個 QA 測試以 `subprocess.Popen([sys.executable, "-m", "studio.server"], env=env)` 啟動真實 server，但 **fixture 沒有 teardown 去終止該進程**：

| 測試檔 | 起 server (Popen) | teardown 清理 |
|---|---|---|
| `test_qa_task1_server_boot.py` | ✅ | ❌ 無 |
| `test_qa_task4_persistence.py` | ✅ | ❌ 無 |
| `test_qa_task6_offline_demo.py` | ✅ | ❌ 無 |

後果：

1. 每個測試起的 server 子進程在測試結束後**不會被殺掉**，持續殘留。
2. server 監聽的 port 取決於 `config.PORT`（讀 `STUDIO_PORT` 環境變數，預設 `8765`）。殘留進程持續佔用 port。
3. 後續測試（如 task1）嘗試啟動新 server 時，**port 被殘留進程佔用 → 啟動失敗 → 探針連不上 → 整批斷言失敗**。

> 本質：測試缺乏進程隔離與資源清理，跑的順序一旦讓「起 server 不清理」的測試排在 task1 之前，task1 就被波及。

---

## 重現步驟

```bash
# 乾淨環境
python3 -m venv /tmp/v && /tmp/v/bin/pip install -e .

# 1) 單獨跑 → 全綠
/tmp/v/bin/python -m pytest tests/test_qa_task1_server_boot.py -q
#   → 7 passed

# 2) 完整套件跑 → task1 全紅（server 起不來）
cd /opt/ti-autopilot-work && /tmp/v/bin/python -m pytest tests/ -q
#   → 656 passed, 8 failed, 1 skipped；8 failed 全在 task1
```

---

## 修復建議

### 方案 A（必做）：為每個起 server 的 fixture 補 teardown

將 fixture 改為 yield 形式，並在結束時終止進程：

```python
@pytest.fixture
def host_port():
    host = "127.0.0.1"
    port = _free_port()
    env = dict(os.environ)
    env.setdefault("STUDIO_TOKEN", "test-token")
    env["STUDIO_PORT"] = str(port)   # 確實把隨機 port 傳給 server，避免共用固定 8765
    proc = subprocess.Popen([sys.executable, "-m", "studio.server"], env=env)
    try:
        assert _wait_until_ready(host, port), "server 未能在期限內就緒"
        yield host, port
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
```

要點：
- `try/finally` 確保**即使測試失敗也會清理**進程。
- 顯式 `env["STUDIO_PORT"] = str(port)`，讓每個 server 用**獨立隨機 port**，從根本消除 port 衝突。
- `terminate → wait → kill` 兩段式關閉，避免殭屍進程。

### 方案 B（建議搭配）：共用 fixture，避免重複

task1/task4/task6 各自重複實作 server 啟動。建議抽到 `tests/conftest.py` 的單一 `live_server` fixture，統一啟動與清理邏輯，杜絕「有人補 teardown、有人忘了」。

### 方案 C（防呆）：session 級殘留進程清理

在 `conftest.py` 加 session-scoped 的 autouse fixture，於測試結束掃描並清理殘留的 `studio.server` 進程，作為最後防線。

---

## 附帶觀察（建議一併處理）

1. **測試檔品質**：`test_qa_task1_server_boot.py` 等檔案存在大量**重複的 import、重複的 assert、字面 `...` 佔位、連續數十行重複 `return False`**，疑似自動生成工具產出未清理。建議重整這些檔案，提升可維護性。
2. **已在 #4 任務順手修掉的兩個相關真 bug**（已落地，非本 issue 範圍，僅記錄）：
   - `test_settings_page_reachable` 原斷言 `/api/settings` 回 200，但 `server.py` 無此路由 → 已改為「server 可達」語義。
   - `studio/config.py` 的 `HOST = os.getenv("STUDIO_PORT", ...)` 誤讀環境變數 → 已修正為 `STUDIO_HOST`。

---

## 驗收標準

1. task1/task4/task6 的 server fixture 皆有 teardown，測試結束後無殘留 `studio.server` 進程。
2. 每個 server 使用獨立隨機 port（`STUDIO_PORT` 由 fixture 注入），不再共用固定 8765。
3. `pytest tests/` 完整套件可乾淨綠燈（除既有的沙箱硬限制測試，如 bwrap/PID 隔離，需另記為已知跳過）。
4. 連續多次重跑完整套件結果穩定（無 flaky）。

---

*本 issue 與 uvicorn 版本鎖定無因果關係，是獨立的測試基礎設施缺陷；列此追蹤，避免「合跑紅燈」長期掩蓋真實回歸訊號。*
