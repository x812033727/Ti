"""跨場次教訓庫 —— 工作室的長期記憶。

每場討論的檢討會蒸餾出可重用的「教訓」（踩過的坑、有效做法、技術選型結論），
持久化到單一 JSON 檔；下次新討論開場時注入 PM 拆解，讓工作室跨場次自我加強——
避免重蹈覆轍、善用既有結論。

存法與 backlog 一致：單一 JSON 檔 + 檔案鎖序列化 read-modify-write，讓多個 session
程序安全增寫。純檔案 IO、與 LLM 解耦，方便單元測試（測試時用 TI_LESSONS_FILE 指向 tmp）。
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import math
import re
import time
from pathlib import Path

from . import config

# 檔案最多保留幾筆（由新到舊截斷），封住長跑下只增不減。注入時另以 LESSONS_MAX 取最新 N 筆。
_MAX_STORE = 500


def _path() -> Path:
    return config.LESSONS_FILE


def _lock_path() -> Path:
    return config.LESSONS_FILE.with_suffix(".lock")


@contextlib.contextmanager
def _locked():
    """以獨立 lock 檔序列化 read-modify-write，跨程序安全。"""
    _path().parent.mkdir(parents=True, exist_ok=True)
    lock = _lock_path().open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


def _load() -> dict:
    p = _path()
    if not p.is_file():
        return {"lessons": []}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("lessons"), list):
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return {"lessons": []}


def _save(data: dict) -> None:
    _path().parent.mkdir(parents=True, exist_ok=True)
    tmp = _path().with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(_path())


def add_many(texts: list[str], *, session_id: str = "", requirement: str = "") -> int:
    """批次新增教訓（對既有內容去重），回傳實際新增數。

    去重採「全文（去前後空白）完全相符」：同一句教訓只留一筆，避免每場重提把庫塞爆。
    """
    cleaned = [t.strip() for t in texts if t and t.strip()]
    if not cleaned:
        return 0
    with _locked():
        data = _load()
        existing = {item["text"].strip() for item in data["lessons"]}
        n = 0
        for text in cleaned:
            if text in existing:
                continue
            data["lessons"].append(
                {
                    "text": text,
                    "session_id": session_id,
                    "requirement": (requirement or "")[:200],
                    "created_at": time.time(),
                }
            )
            existing.add(text)
            n += 1
        if n:
            # 只保留最新 _MAX_STORE 筆（依出現序，新的在尾端）。
            data["lessons"] = data["lessons"][-_MAX_STORE:]
            _save(data)
        return n


def recent(limit: int) -> list[dict]:
    """取最新 limit 筆教訓（由新到舊）。limit <= 0 回空清單。"""
    if limit <= 0:
        return []
    return list(reversed(_load()["lessons"]))[:limit]


def _tokens(text: str) -> set[str]:
    """中英混合輕量斷詞：ASCII 詞 + 中文字元 bigram。

    不引入任何斷詞/embedding 依賴；bigram 對中文的主題比對已足夠
    （「無人機」→ {無人, 人機} 不會撞上「網站後台」的任何 bigram）。
    """
    text = text.lower()
    words = set(re.findall(r"[a-z0-9_]+", text))
    han = re.findall(r"[一-鿿]", text)
    bigrams = {a + b for a, b in zip(han, han[1:], strict=False)}
    return words | bigrams


def relevant(limit: int, requirement: str) -> list[dict]:
    """取與需求最相關的 limit 筆教訓（IDF 加權重疊分數降冪、同分新者優先）。

    做多種產品後教訓庫會混雜（無人機的坑不該注入網站任務）——按相關性挑選而非
    「最新 N 筆」。token 以庫內文件頻率做 IDF 加權：「做一」「一個」這類滿庫都是的
    泛用詞自動降權，主題詞（「無人」「人機」）自然勝出，無需維護停用詞表。
    完全無相關（全部 0 分）時回空清單，由呼叫端退回最新 N 筆。
    """
    if limit <= 0:
        return []
    items = _load()["lessons"]
    q = _tokens(requirement)
    if not q or not items:
        return []
    toks = [_tokens(f"{it.get('text', '')} {it.get('requirement', '')}") for it in items]
    df: dict[str, int] = {}
    for lt in toks:
        for t in lt:
            df[t] = df.get(t, 0) + 1
    n = len(items)

    def _score(lt: set[str]) -> float:
        return sum(math.log(1 + n / df[t]) for t in q & lt)

    scored = [(s, it) for it, lt in zip(items, toks, strict=True) if (s := _score(lt)) > 0]
    scored.sort(key=lambda p: (p[0], p[1].get("created_at", 0)), reverse=True)
    return [it for _, it in scored[:limit]]


def context(limit: int | None = None, requirement: str = "") -> str:
    """組成要注入 PM 拆解 prompt 的教訓區塊；停用、無教訓或 limit<=0 時回 ""。

    有給 requirement 時優先按相關性挑選（避免跨領域教訓互相污染），
    完全無相關或未給需求則退回「最新 N 筆」（原行為）。
    """
    if not config.LESSONS_ENABLED:
        return ""
    cap = config.LESSONS_MAX if limit is None else limit
    rows = relevant(cap, requirement) if requirement.strip() else []
    picked_by_relevance = bool(rows)
    if not rows:
        rows = recent(cap)
    if not rows:
        return ""
    body = "\n".join(f"- {r['text']}" for r in rows)
    note = "依本次需求相關性挑選" if picked_by_relevance else "最新數筆"
    return (
        f"【跨場次教訓庫（過往各場討論檢討蒸餾，{note}；請避免重蹈覆轍、善用既有結論）】\n"
        f"{body}\n\n"
    )


def all_lessons() -> list[dict]:
    """回傳全部教訓（依儲存序，舊→新）；供檢視 / 測試。"""
    return list(_load()["lessons"])
