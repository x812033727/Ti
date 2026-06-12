"""架構決策記錄（ADR）—— 把辯論/設計結論固化成可查的決策檔。

架構辯論與架構師定案的結論原本只活在當場 events / 記憶體 context，session 結束即消失，
後續場次翻案無據。本模組把結論蒸餾成決策條目落盤在 workspace 根：
  DECISIONS.md   人讀（append 渲染；進檔案面板、git 歷史與交付物）
  adr.json       機讀索引（檔案鎖序列化寫入）
專案模式共用固定 workspace，決策自動跨場累積；後續場次注入摘要、翻案須說明理由。

以 cwd 定位（同 NOTES.md 模式），orchestrator 不需知道 project id；一次性 session 也無害。
純檔案 IO、與 LLM 解耦，方便單元測試。
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import re
import time
from pathlib import Path

from . import config

# 行標記解析：`決策:` 與既有 ARCHITECT 格式 `設計決策:` 統一收（roles.py 的定案輸出）；
# `理由:` / `否決:` 為可選補充行，附掛在前一條決策上。
_RE_DECISION = re.compile(r"^\s*(?:設計)?決策\s*[:：]\s*(.+?)\s*$")
_RE_RATIONALE = re.compile(r"^\s*理由\s*[:：]\s*(.+?)\s*$")
_RE_REJECTED = re.compile(r"^\s*否決\s*[:：]\s*(.+?)\s*$")


def _json_path(cwd: Path) -> Path:
    return Path(cwd) / "adr.json"


def _md_path(cwd: Path) -> Path:
    return Path(cwd) / "DECISIONS.md"


@contextlib.contextmanager
def _locked(cwd: Path):
    """以獨立 lock 檔序列化 read-modify-write，跨程序安全。"""
    lock = (Path(cwd) / "adr.lock").open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


def _load(cwd: Path) -> dict:
    p = _json_path(cwd)
    if not p.is_file():
        return {"entries": []}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("entries"), list):
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return {"entries": []}


def parse_adr(text: str) -> list[dict]:
    """解析專家輸出成決策條目；`理由:`/`否決:` 行附掛在前一條決策上。

    無任何決策行回空清單（呼叫端不落盤）——與其他行標記解析同為「失敗即降級」。
    """
    entries: list[dict] = []
    cur: dict | None = None
    for line in (text or "").splitlines():
        m = _RE_DECISION.match(line)
        if m:
            cur = {"decision": m.group(1), "rationale": "", "rejected": ""}
            entries.append(cur)
            continue
        m = _RE_RATIONALE.match(line)
        if m and cur is not None:
            cur["rationale"] = m.group(1)
            continue
        m = _RE_REJECTED.match(line)
        if m and cur is not None:
            cur["rejected"] = m.group(1)
    return entries


def record(cwd: Path | None, entries: list[dict], *, session_id: str = "") -> int:
    """把決策條目寫進 adr.json 並 append 到 DECISIONS.md，回實際新增數。

    去重採「決策全文（去前後空白）完全相符」（同 lessons 慣例），避免每場重提塞爆。
    cwd 為 None（無 workspace 的單元測試）或無條目時直接回 0。
    """
    if cwd is None or not entries:
        return 0
    cleaned = [e for e in entries if (e.get("decision") or "").strip()]
    if not cleaned:
        return 0
    with _locked(cwd):
        data = _load(cwd)
        existing = {e["decision"].strip() for e in data["entries"]}
        added: list[dict] = []
        for e in cleaned:
            text = e["decision"].strip()
            if text in existing:
                continue
            entry = {
                "decision": text,
                "rationale": (e.get("rationale") or "").strip(),
                "rejected": (e.get("rejected") or "").strip(),
                "session_id": session_id,
                "created_at": time.time(),
            }
            data["entries"].append(entry)
            existing.add(text)
            added.append(entry)
        if not added:
            return 0
        tmp = _json_path(cwd).with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(_json_path(cwd))
        _append_md(cwd, added)
        return len(added)


def _append_md(cwd: Path, entries: list[dict]) -> None:
    """把新決策 append 進 DECISIONS.md（人讀；落檔失敗不擋流程，同 _write_prd 語意）。"""
    path = _md_path(cwd)
    lines = [] if path.exists() else ["# 架構決策記錄（ADR）\n"]
    stamp = time.strftime("%Y-%m-%d %H:%M")
    for e in entries:
        lines.append(f"## {e['decision']}")
        lines.append(f"- 時間：{stamp}")
        if e["rationale"]:
            lines.append(f"- 理由：{e['rationale']}")
        if e["rejected"]:
            lines.append(f"- 否決方案：{e['rejected']}")
        lines.append("")
    try:
        with path.open("a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except OSError:
        pass


def context(cwd: Path | None, limit: int | None = None) -> str:
    """組成要注入 prompt 的既有決策區塊；停用、無 cwd 或無決策時回 ""。"""
    if not config.ADR_ENABLED or cwd is None:
        return ""
    cap = config.ADR_MAX if limit is None else limit
    if cap <= 0:
        return ""
    rows = _load(cwd)["entries"][-cap:]
    if not rows:
        return ""
    lines = ["【既有架構決策（除非有明確新理由，請沿用；翻案須說明理由）】"]
    for e in rows:
        suffix = f"（理由：{e['rationale']}）" if e.get("rationale") else ""
        lines.append(f"- {e['decision']}{suffix}")
    return "\n".join(lines) + "\n\n"


def all_entries(cwd: Path) -> list[dict]:
    """回傳全部決策（依儲存序，舊→新）；供檢視 / 測試。"""
    return list(_load(cwd)["entries"])
