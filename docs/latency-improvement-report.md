# 延遲改善前後對比報告

**日期**：2026-07-11
**任務**：#3 產出前後對比報告
**基準 commit**：`c3a93669`

## 一、結論

本輪只完成**離線層可證明的 prompt 注入**：所有 8 個內建角色的 system prompt 已加入「自由散文 500 字內」指示，並明確豁免結構化 marker 行與必要條列內容。

**真環境行為生效尚未驗證**：本輪未打真 API，沒有可比較的真實 `latency.by_role`、`token_usage.by_role` 或輸出 token 降幅。此變更是軟性 prompt 指示，不是 provider `max_tokens` 硬上限。

## 二、離線層：前後差異

### 2.1 指示注入 diff

`studio/roles.py::_COMMON` 原本只有 1 至 3 條共用規則；目前新增第 4 條：

```diff
 "1. 一律用繁體中文發言。\n"
 "2. 發言精簡、聚焦，不要長篇大論；像在團隊會議裡講重點。\n"
 "3. 你和同事共用同一個工作目錄（你的 cwd），檔案會即時被別人看到。\n"
+"4. 單則發言的自由散文部分限 500 字內；結構化標記行"
+"（如 `任務:`/`驗證:`/`決議:`/`依賴:`/`後續任務:`/`核心改動:`）"
+"及其必要條列內容不計入。\n"
```

對比指令：

```bash
git diff --no-ext-diff --unified=4 c3a93669^1 c3a93669 -- studio/roles.py tests/core/test_roles.py
```

### 2.2 8 角色覆蓋證據

守門測試新增於 `tests/core/test_roles.py`，對 `roles.BUILTIN_ROLES`（import 期凍結的 8 角色）逐一檢查以下片段：

- `單則發言的自由散文部分限 500 字內`
- `結構化標記行`
- `` `任務:`/`驗證:`/`決議:` ``
- `` `依賴:`/`後續任務:`/`核心改動:` ``
- `必要條列內容不計入`

本地查核輸出：

```text
builtin_roles: 8
role_keys: pm,engineer,qa,senior,researcher,architect,security,devops
missing: {}
```

可重跑指令：

```bash
timeout 60 .venv/bin/python -c 'from studio import roles
fragments=("單則發言的自由散文部分限 500 字內","結構化標記行","`任務:`/`驗證:`/`決議:`","`依賴:`/`後續任務:`/`核心改動:`","必要條列內容不計入")
print("builtin_roles:", len(roles.BUILTIN_ROLES))
print("role_keys:", ",".join(r.key for r in roles.BUILTIN_ROLES))
missing={r.key:[f for f in fragments if f not in r.system_prompt] for r in roles.BUILTIN_ROLES}
print("missing:", {k:v for k,v in missing.items() if v})'
```

## 三、離線自測結果

本輪驗證的是 prompt 注入與 guard test，不宣稱全套件驗收口徑變更。
此 checkout 沒有 `.venv/bin/python`，本輪實跑使用專案根目錄的 `./python` 包裝器。

```bash
timeout 300 .venv/bin/python -m pytest -q tests/core/test_roles.py
```

預期：`10 passed`。

本輪實跑結果：`10 passed in 1.76s`。

## 四、真環境層：N/A

目前沒有可採信的前後真實 latency 數據：

- 前置 history 曾實查為 latency / token_usage 全零，不能拿 fake 或 improver 殘影推估成效。
- 本輪未打真 API，輸出縮短效果待補驗。
- 本輪未跑真 provider session，沒有新增真實 `token_usage` 事件。
- 本 checkout 的 gitignored `history/` 是可變 runtime 目錄，完整測試可能追加 improver 殘影；doc test 僅能重放「全零前置數據不成立」，不能證明真環境生效。
- 500 字規則只影響模型行為傾向；實際輸出 token 是否下降，必須由真 API 事件驗證。

驗收時若出現以下狀況，該輪補驗不合格：

- `token_usage.total.calls == 0`
- `latency.total.count == 0`
- `token_usage.by_provider` 只有 `fake`
- `latency.by_role` 或 `token_usage.by_role` 為空

## 五、真環境補驗指令

以下流程請在有真 provider 憑證的環境跑兩次：一次在改動前基準，一次在目前 commit。兩次使用同一段 requirement，再比較輸出的 `completion` 與 `avg_completion_per_call`。

### 5.1 跑一場真 session

```bash
TI_OFFLINE=0 TI_ACCESS_PASSWORD= TI_PROVIDER=claude timeout 300 .venv/bin/python - <<'PY'
from fastapi.testclient import TestClient
from studio.server import app

requirement = "做一個最小 CLI，讀入兩個數字並輸出總和；只需一個任務。"
events = []
with TestClient(app, client=("127.0.0.1", 12345)).websocket_connect("/ws") as ws:
    ws.send_json({"requirement": requirement})
    for _ in range(800):
        ev = ws.receive_json()
        events.append(ev)
        if ev.get("type") in ("done", "error"):
            break

last = events[-1]
print("session_id:", last.get("session_id"))
print("last_type:", last.get("type"))
print("completed:", (last.get("payload") or {}).get("completed"))
PY
```

若使用其他真 provider，替換 `TI_PROVIDER` 與必要金鑰環境變數；不可用 `TI_OFFLINE=1`。

### 5.2 讀最新 meta 的 by_role latency 與 output tokens

```bash
timeout 60 .venv/bin/python - <<'PY'
import glob
import json
from pathlib import Path

metas = sorted(glob.glob("history/*.meta.json"), key=lambda p: Path(p).stat().st_mtime)
if not metas:
    raise SystemExit("找不到 history/*.meta.json")

path = metas[-1]
with open(path, encoding="utf-8") as f:
    meta = json.load(f)

lat = meta.get("latency", {})
usage = meta.get("token_usage", {})
print("meta:", path)
print("latency.total:", lat.get("total"))
print("token_usage.total:", usage.get("total"))

roles = sorted(set(lat.get("by_role", {})) | set(usage.get("by_role", {})))
for role in roles:
    l = lat.get("by_role", {}).get(role, {})
    u = usage.get("by_role", {}).get(role, {})
    calls = u.get("calls", 0)
    completion = u.get("completion", 0)
    avg_completion = round(completion / calls, 1) if calls else 0
    print(
        f"{role}: latency_calls={l.get('count', 0)} avg_ms={l.get('avg_ms', 0)} "
        f"token_calls={calls} completion={completion} avg_completion_per_call={avg_completion}"
    )
PY
```

### 5.3 判讀方式

前後比較以角色為單位：

- 主要看 `avg_completion_per_call` 是否下降。
- 次要看 `latency.by_role.avg_ms` 是否跟著下降。
- 若 calls 數或任務路徑差太多，該次比較不採信，重跑同一 requirement。
- 若 token 下降但 latency 未下降，需另查 provider 排隊、cache、模型差異，不可把本輪變更判為無效。

## 六、範圍聲明

本報告只覆蓋 `studio/roles.py` 的 prompt 指示與 `tests/core/test_roles.py` 的離線守門；未改 `config.py`、模型綁定、provider、快取或 token 硬上限。
