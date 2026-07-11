# Prompt Cache 選題依據

## 結論

本輪不使用「找最慢角色」作為選題依據：既有 history meta 的 latency 與 token_usage 聚合值全為 0，沒有可用的真 API 時延或 token 樣本。選題改採兩個事實：研究調研指出 Claude Code subagent/SDK 路徑可能未啟用 prompt caching；既有 meta 的 `cache_read` / `cache_write` 欄位也全為 0，代表目前看不到任何快取命中或寫入。

因此本輪只選一項改善：啟用 Claude prompt caching env。命中證據目前是 N/A；上線後應以真 session 的 `/api/metrics` cache 欄位補驗。

## History Meta 聚合

`history/` 是 `.gitignore` 排除的本機執行資料，不隨 lane 版控。本文件採用 PM 開場查核的樣本結論：200 場 meta 全部含 `latency`，但 `latency` 與 `token_usage` 所有加總欄位皆為 0。本 lane 於 2026-07-11 查核 `config.HISTORY_ROOT=/opt/ti-autopilot-work.lanes/lane-ap884b8f9ffc-1/history`，目錄不存在，因此未重造或補寫假資料。

可重跑聚合指令：

```bash
timeout 60 .venv/bin/python - <<'PY'
import json
import os
from pathlib import Path

root = Path(os.getenv("TI_HISTORY_ROOT", "history"))
metas = []
for path in sorted(root.glob("*.meta.json")):
    metas.append(json.loads(path.read_text(encoding="utf-8")))

def nz(value):
    if isinstance(value, dict):
        return any(nz(v) for v in value.values())
    if isinstance(value, list):
        return any(nz(v) for v in value)
    return bool(value)

token_fields = ("prompt", "completion", "total", "cost_usd", "calls", "cache_read", "cache_write")
token_total = {k: 0 for k in token_fields}
latency_total = {"count": 0, "sum_ms": 0, "max_ms": 0}
nonzero = []

for meta in metas:
    usage = ((meta.get("token_usage") or {}).get("total") or {})
    latency = ((meta.get("latency") or {}).get("total") or {})
    for key in token_fields:
        token_total[key] += usage.get(key, 0) or 0
    for key in latency_total:
        latency_total[key] += latency.get(key, 0) or 0
    if nz(meta.get("token_usage")) or nz(meta.get("latency")):
        nonzero.append(meta.get("session_id"))

print(json.dumps({
    "history_root": str(root),
    "meta_files": len(metas),
    "with_latency": sum(1 for m in metas if "latency" in m),
    "with_token_usage": sum(1 for m in metas if "token_usage" in m),
    "latency_total": latency_total,
    "token_usage_total": token_total,
    "nonzero_meta_files": nonzero,
}, ensure_ascii=False, indent=2))
PY
```

PM 開場查核摘要：

```json
{
  "meta_files": 200,
  "with_latency": 200,
  "with_token_usage": 200,
  "latency_total": {"count": 0, "sum_ms": 0, "max_ms": 0},
  "token_usage_total": {
    "prompt": 0,
    "completion": 0,
    "total": 0,
    "cost_usd": 0,
    "calls": 0,
    "cache_read": 0,
    "cache_write": 0
  },
  "nonzero_meta_files": []
}
```

解讀：這只能證明「本 clone 沒有真 API latency/token 可分析」與「快取欄位目前沒有命中紀錄」，不能宣稱任何角色最慢。

## Prompt 長度門檻

Sonnet / Opus prompt caching 的最低快取長度為 1024 token；Haiku 4.5 是 4096 token，本輪不以 Haiku 門檻作為保證。由於本環境沒有 `ANTHROPIC_API_KEY`，不能呼叫官方 `messages.count_tokens` 精算；以下為離線估算。

可重跑指令：

```bash
timeout 60 .venv/bin/python - <<'PY'
from pathlib import Path
from studio import conventions
from studio.roles import BUILTIN_ROLES

print("| role | raw_chars | raw_bytes | rendered_chars | rendered_bytes | allowed_tools |")
print("|---|---:|---:|---:|---:|---:|")
for role in sorted(BUILTIN_ROLES, key=lambda r: len(conventions.apply(r, Path.cwd()).system_prompt)):
    rendered = conventions.apply(role, Path.cwd()).system_prompt
    print(
        f"| {role.key} | {len(role.system_prompt)} | {len(role.system_prompt.encode('utf-8'))} | "
        f"{len(rendered)} | {len(rendered.encode('utf-8'))} | {len(role.allowed_tools)} |"
    )
PY
```

本 lane 輸出：

| role | raw_chars | raw_bytes | rendered_chars | rendered_bytes | allowed_tools |
|---|---:|---:|---:|---:|---:|
| devops | 414 | 1072 | 1044 | 2106 | 3 |
| architect | 430 | 1184 | 1060 | 2218 | 3 |
| researcher | 438 | 1136 | 1068 | 2170 | 4 |
| security | 447 | 1207 | 1077 | 2241 | 3 |
| senior | 505 | 1343 | 1135 | 2377 | 4 |
| qa | 543 | 1303 | 1173 | 2337 | 6 |
| pm | 780 | 1952 | 1410 | 2986 | 2 |
| engineer | 798 | 1964 | 1428 | 2998 | 6 |

`raw_*` 是 `roles.py` 的 `_COMMON + 角色 prompt`；`rendered_*` 是實際建構 `Expert` 時經 `conventions.apply()` 附加執行慣例卡後的可見 system prompt。若只把 `roles.py` 原始 `raw_chars` 當成快取斷點，最短角色不足以作為嚴格保證；本輪採 env 啟用 CLI 既有快取機制，不自行插 `cache_control`。

實際 Claude Code prefix 還包含允許工具 schema。本機 Claude Code 2.1.207 附帶 `sdk-tools.d.ts`，可作工具 schema 長度 proxy；估算公式採「CJK 字元 1 token、其餘字元 4 chars/token」。最短角色 `devops` 仍估 1138 token，超過 Sonnet / Opus 的 1024 token 門檻。

可重跑 schema proxy 估算：

```bash
timeout 60 .venv/bin/python - <<'PY'
from pathlib import Path
import shutil

from studio import conventions
from studio.roles import BUILTIN_ROLES

claude = Path(shutil.which("claude")).resolve()
schema = (claude.parents[1] / "sdk-tools.d.ts").read_text(encoding="utf-8")
name_map = {
    "Bash": "BashInput",
    "Read": "FileReadInput",
    "Glob": "GlobInput",
    "Grep": "GrepInput",
    "Edit": "FileEditInput",
    "Write": "FileWriteInput",
    "WebFetch": "WebFetchInput",
    "WebSearch": "WebSearchInput",
}

def chunk(tool):
    iface = name_map.get(tool)
    if not iface:
        return ""
    start = schema.index(f"export interface {iface}")
    end = schema.find("\nexport ", start + 1)
    return schema[start : end if end != -1 else len(schema)]

def est_tokens(text):
    han = sum("\u4e00" <= c <= "\u9fff" for c in text)
    return round(han + (len(text) - han) / 4)

for role in BUILTIN_ROLES:
    rendered = conventions.apply(role, Path.cwd()).system_prompt
    proxy = "".join(chunk(tool) for tool in role.allowed_tools)
    print(role.key, len(rendered), len(proxy), est_tokens(rendered + proxy))
PY
```

| role | rendered_chars | tool_schema_proxy_chars | conservative_est_tokens |
|---|---:|---:|---:|
| devops | 1044 | 2181 | 1138 |
| researcher | 1068 | 3390 | 1466 |
| architect | 1060 | 3345 | 1466 |
| pm | 1410 | 2936 | 1590 |
| security | 1077 | 4201 | 1688 |
| senior | 1135 | 4610 | 1828 |
| qa | 1173 | 5194 | 1956 |
| engineer | 1428 | 5194 | 2160 |

## 樣本限制

- 無真 API latency/token 數據，命中證據 N/A。
- history 200 場全零結論來自 PM 開場實查；此 lane 未包含被 `.gitignore` 排除的 `history/` 原始檔。
- token 數是離線估算；正式精算需在有 API key 的環境用 `messages.count_tokens` 或真 session 的 `/api/metrics` 補驗。
- 不使用 fake provider 或 dry-run 數據作為成效證明。
