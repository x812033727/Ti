# Follow-up Issue #0001：落地 `forwarded_allow_ips` / `proxy_headers`，完成 ProxyHeaders 防偽造

- **狀態**：Closed（2026-06-12 落地，見文末「落地摘要」）
- **優先級**：High（安全強化，目前實質防護為零）
- **類型**：Security / Hardening
- **建立日期**：2026-06-09
- **前置任務**：uvicorn 版本鎖定 `>=0.30,<0.50`（已完成，僅建立版本基線）
- **相關檔案**：`studio/server.py:51`、`pyproject.toml:14-16`

---

## 背景

前一個任務已將 `uvicorn[standard]` 鎖到 `>=0.30,<0.50`，並在 `pyproject.toml` 加註解，建立了「ProxyHeaders 信任鏈強化」的**版本基線**。

但版本鎖**只是基線，不等於防護生效**。`studio/server.py:51` 目前的啟動寫法是：

```python
uvicorn.run(app, host=config.HOST, port=config.PORT)
```

未傳入 `proxy_headers` / `forwarded_allow_ips`，因此：

- uvicorn 的 `ProxyHeadersMiddleware` 仍以**預設信任來源 `127.0.0.1`** 運作。
- **對「X-Forwarded-For 取最左值偽造」的實質防護目前為零** —— 版本升上去了，但執行設定沒收斂，攻擊面並未真正關閉。

> 結論：本 issue 的目標是把「版本基線」轉為「實際防護」。

---

## 問題說明

`ProxyHeadersMiddleware` 只會信任 `forwarded_allow_ips` 清單內來源送來的 `X-Forwarded-*` 標頭。風險本質在**信任設定**而非版本：

- 若 `forwarded_allow_ips` 設得太寬（尤其 `"*"`），攻擊者可自帶 `X-Forwarded-For` **偽造 client IP**，污染日誌、稽核、限流、IP 白名單等所有依賴 client IP 的邏輯。
- 官方明確警告：**只信任你真的能信任的來源**，嚴禁 `"*"`。

---

## 驗收標準

1. `studio/server.py` 的 `uvicorn.run(...)` 明確傳入 `proxy_headers=True` 與 `forwarded_allow_ips=<可信來源>`。
2. `forwarded_allow_ips` 的值由設定／環境變數提供（如 `config.FORWARDED_ALLOW_IPS`），**預設為安全值**（本機或私網範圍），**程式內嚴禁硬編 `"*"`**。
3. 部署文件說明：值應設為負載平衡器／proxy 的私網範圍（例如 `10.0.0.0/8,172.16.0.0/12,192.168.0.0/16`），並提醒 proxy 端先 strip 外部傳入的 `X-Forwarded-*`（雙重防線）。
4. 評估並記錄是否升級到 `uvicorn>=0.31`（見下方）。
5. 新增測試：驗證 `forwarded_allow_ips` 不為 `"*"`、且 `proxy_headers` 已啟用。

---

## 升級評估：是否提升下限至 `>=0.31`

研究結論：**「徹底避免最左值偽造 + 支援 CIDR 信任網段」的中介軟體重大強化在 uvicorn 0.31.0（2024-09-27）才完整落地**（PR #2231 → #2468），包含：

- 信任 IP **網段（CIDR）** 支援
- **IPv6** 支援
- 修正本機 proxy 來源與空標頭 fallback

影響：

- K8s / Docker Swarm 等 **proxy IP 會變動** 的環境，需 `>=0.31` 才能用 CIDR 信任網段；`0.30` 只能列舉固定 IP。
- 本專案環境實裝已是 `0.49.0`，落在 `>=0.30,<0.50` 內，技術上已具備 0.31+ 能力；建議**將 pyproject 下限提升至 `>=0.31`**，讓「需要 CIDR」這件事在依賴宣告層面被保證，避免日後有人降到 0.30.x 而功能默默失效。

---

## 建議實作方向（草案）

```python
# studio/config.py
# 預設僅信任本機；部署端以環境變數覆寫為 proxy 私網範圍，嚴禁 "*"
FORWARDED_ALLOW_IPS = os.getenv("FORWARDED_ALLOW_IPS", "127.0.0.1")
```

```python
# studio/server.py:51
uvicorn.run(
    app,
    host=config.HOST,
    port=config.PORT,
    proxy_headers=True,
    forwarded_allow_ips=config.FORWARDED_ALLOW_IPS,
)
```

啟動時可加一道保護：偵測到 `forwarded_allow_ips == "*"` 時記 warning 或直接拒絕啟動。

---

## 範圍與非目標

- **範圍**：`server.py` 啟動設定、`config` 新增設定項、部署文件、相關測試、`>=0.31` 升級評估。
- **非目標**：不在本 issue 引入 poetry/uv 等相依管理工具；不重構 server 其他啟動邏輯。

---

## 補充：CI 監控建議

上限 `<0.50` 是對齊現狀的權宜值，日後仍會被新版超過。建議 CI 加 `pip-audit`／Snyk 持續監控 uvicorn 漏洞，取代「靠手動調上限」的方式。

---

*本 issue 由前次「uvicorn 版本鎖定」任務的架構決策衍生，用於追蹤實際防護落地，確保此安全風險不懸空消失。*

---

## 落地摘要（2026-06-12）

| 驗收標準 | 落地 |
|---|---|
| 1. `uvicorn.run` 傳 `proxy_headers=True` + `forwarded_allow_ips` | `studio/server.py` `main()` 已傳入 |
| 2. 值由 env 提供、預設安全值、嚴禁硬編 `"*"` | `studio/config.py` `FORWARDED_ALLOW_IPS`（env `TI_FORWARDED_ALLOW_IPS`，別名 `FORWARDED_ALLOW_IPS`，預設 `127.0.0.1`）＋ `forwarded_allow_ips()` 偵測 `"*"` 即 `SystemExit` fail-closed |
| 3. 部署文件 | `README.md` 新增「反向代理部署（X-Forwarded 信任鏈）」小節（傳輸層 vs 應用層兩層對照、proxy 端 strip、私網範圍）；`.env.example` 補段 |
| 4. 升級評估 `>=0.31` | 已採納：`pyproject.toml` 下限升至 `uvicorn[standard]>=0.31,<0.50`，ci.yml 兩處同步 |
| 5. 新增測試 | `tests/server/test_forwarded_allow_ips.py`（預設本機、env 覆寫與別名、`"*"` 拒啟動、main() kwargs 截獲斷言、pyproject 下限 `>=0.31`） |

註：`TI_FORWARDED_ALLOW_IPS`（傳輸層，uvicorn ProxyHeadersMiddleware）與既有
`TI_TRUST_PROXY`/`TI_TRUSTED_PROXIES`（應用層，`studio/netutil.py` 解析 XFF）語意獨立、各自設定。
CI 監控建議（pip-audit/Snyk）仍列為後續強化，不在本 issue 範圍。
