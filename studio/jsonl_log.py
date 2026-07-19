"""通用 append-only JSONL 日誌(第 3 階信任指標 A0 的落檔基座)。

audit.jsonl 的 append/壓實範式(autopilot._append_audit)已在生產驗證,本模組把同一套
不變量抽成可重用原語,供 interventions.jsonl(人工介入)與 events.jsonl(系統事件)使用
——不回頭改 audit 熱路徑(單一 writer 慣例不動,避免動到主迴圈)。

不變量(對齊 audit 範式):
- 首建走 secure_write_root(root owner,REQUIRE_CHOWN 不變量),之後 open("a") 逐行 append。
- 超過大小門檻即壓實:保留期外舊紀錄搬 .old 冷歸檔、現役檔原子重寫;壞行(解析不出 ts)
  視為舊紀錄一併歸檔;全部在保留期內則不動(寧可暫時超標,不丟計數窗口內的紀錄)。
- 任何寫入失敗只 debug log,絕不冒泡——可觀測性是加值,不得影響呼叫端。
- 多行程 append 依賴 O_APPEND 單行原子性(單行遠小於 PIPE_BUF)。壓實的原子重寫會蓋掉
  「快照後才 append」的紀錄——events.jsonl 有 web/autopilot 雙行程寫入是真實情境,故
  重寫前複查檔案大小,快照後有新寫入即放棄本輪(下次再壓);殘餘的 stat→rename 微小
  窗口內仍可能丟單筆,可容忍——這裡只有觀測數據,不進任何控制流。
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from .secure_write import secure_write_root

log = logging.getLogger("ti.jsonl_log")

# 與 audit.jsonl 同款純模組常數(不開 env override):5MB ≈ 數萬筆,正常量級多年才觸發。
MAX_BYTES = 5 * 1024 * 1024
KEEP_DAYS = 30


def append(path: Path, rec: dict) -> None:
    """append 一筆紀錄(自動補 ts);任何失敗吞掉只留 debug log。"""
    try:
        rec.setdefault("ts", time.time())
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            secure_write_root(path, b"")
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        _maybe_compact(path)
    except Exception:  # noqa: BLE001 — 落檔失敗不得影響呼叫端
        log.debug("jsonl append 失敗(忽略):%s", path, exc_info=True)


def read_window(path: Path, days: int) -> list[dict]:
    """讀近 days 天(ts >= now - days*86400)的紀錄;壞行/壞 ts 跳過,檔案不存在=空。"""
    if not path.is_file():
        return []
    cutoff = time.time() - days * 86400
    out: list[dict] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
            if isinstance(rec, dict) and float(rec.get("ts", 0)) >= cutoff:
                out.append(rec)
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
    return out


def _maybe_compact(path: Path) -> None:
    """超過大小門檻時壓實:保留期外舊紀錄搬 .old、現役檔原子重寫(鏡射 audit 範式)。"""
    snapshot_size = path.stat().st_size
    if snapshot_size <= MAX_BYTES:
        return
    cutoff = time.time() - KEEP_DAYS * 86400
    keep: list[str] = []
    old: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ts = float(json.loads(line).get("ts", 0))
        except (ValueError, TypeError):
            ts = 0.0  # 壞行視為舊紀錄歸檔
        (keep if ts >= cutoff else old).append(line)
    if not old:
        return  # 全在保留期內:不重寫(見模組 docstring)
    # 併寫防線:快照後有其他行程 append(檔案又長大)→ 放棄本輪壓實,否則原子重寫會
    # 蓋掉快照後落地的新紀錄。read_text 讀到比 stat 更多內容也算長大,同樣下次再壓。
    if path.stat().st_size != snapshot_size:
        return
    archive = path.with_suffix(path.suffix + ".old")
    if not archive.exists():
        secure_write_root(archive, b"")
    with archive.open("a", encoding="utf-8") as f:
        f.write("\n".join(old) + "\n")
    body = ("\n".join(keep) + "\n").encode("utf-8") if keep else b""
    secure_write_root(path, body)
    log.info("%s 壓實:歸檔 %d 筆、保留 %d 筆(近 %d 天)", path.name, len(old), len(keep), KEEP_DAYS)
