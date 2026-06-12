"""討論流程的純函式層：決議解析、停滯偵測、任務／依賴／教訓解析與波次規劃。

無狀態、不依賴 StudioSession，自 orchestrator.py 抽出（行為逐字不變）。orchestrator.py
以顯式 re-export 保住既有 import 路徑——tests、autopilot、improver 皆 `from studio.orchestrator
import ...`，且對 `studio.orchestrator.<fn>` 的 monkeypatch 仍有效（orchestrator 內部沿用裸名
查找本模組屬性）。對 `studio.flow.<fn>` 的 monkeypatch 不影響 orchestrator 內部呼叫。
"""

from __future__ import annotations

import difflib
import logging
import re

from . import config

log = logging.getLogger("ti.flow")

# --- 決議解析 -----------------------------------------------------------


def _last_match(text: str, pattern: str) -> str | None:
    matches = re.findall(pattern, text)
    return matches[-1].strip() if matches else None


def qa_passed(text: str) -> bool:
    verdict = _last_match(text, r"驗證\s*[:：]\s*(PASS|FAIL)")
    if verdict:
        return verdict.upper() == "PASS"
    # 後備：找不到標記時，看是否出現失敗字樣
    return not re.search(r"\b(fail|failed|error|錯誤|失敗)\b", text, re.I)


def senior_approved(text: str) -> bool:
    verdict = _last_match(text, r"決議\s*[:：]\s*(核可|退回)")
    if verdict:
        return verdict == "核可"
    return not re.search(r"(退回|需修改|必須修正)", text)


def security_approved(text: str) -> bool:
    verdict = _last_match(text, r"決議\s*[:：]\s*(安全核可|安全退回)")
    if verdict:
        return verdict == "安全核可"
    return not re.search(r"(安全退回|高風險|不安全|漏洞|injection)", text, re.I)


def critic_blocks(text: str) -> bool:
    """異議檢查判定：critic 是否提出『成立』的異議（True=需退回，False=放行）。"""
    verdict = _last_match(text, r"異議\s*[:：]\s*(成立|不成立)")
    if verdict:
        return verdict == "成立"
    # 後備：無標記時偏向放行，僅在出現明確反對字樣時才退回，避免誤擋。
    return bool(re.search(r"(異議成立|不應通過|尚未完成|還不算完成)", text))


def text_similarity(a: str, b: str) -> float:
    """兩段文字的相似度（0~1）。用於偵測『只是重述、無實質進展』。"""
    a, b = a.strip(), b.strip()
    if not a and not b:
        return 1.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def is_stalled(history: list[str], rounds: int, threshold: float = 0.9) -> bool:
    """最近 rounds 筆發言彼此高度相似（無實質進展）即視為停滯。

    rounds<=1 或歷史不足 rounds 筆時不判定停滯（避免一開始就誤觸）。
    """
    if rounds <= 1 or len(history) < rounds:
        return False
    recent = history[-rounds:]
    first = recent[0]
    return all(text_similarity(first, t) >= threshold for t in recent[1:])


def pm_done(text: str) -> bool:
    verdict = _last_match(text, r"決議\s*[:：]\s*(完成|未完成)")
    if verdict:
        return verdict == "完成"
    return bool(re.search(r"(已完成|達成|符合驗收)", text))


def parse_tasks(pm_text: str) -> list[str]:
    """從 PM 的拆解文字抽出任務條目。優先 `任務: ...`，否則退回條列項目。"""
    cap = config.MAX_TASKS
    explicit = [m.strip() for m in re.findall(r"^\s*任務\s*[:：]\s*(.+)$", pm_text, re.M)]
    if explicit:
        return explicit[:cap]
    tasks: list[str] = []
    for line in pm_text.splitlines():
        m = re.match(r"^\s*(?:[-*•]|\d+[.)、])\s+(.*)$", line)
        if m:
            item = m.group(1).strip()
            if item and len(item) < 200 and not re.search(r"(執行指令|執行命令)", item):
                tasks.append(item)
    return tasks[:cap] or ["實作需求"]


def parse_clarify(text: str) -> list[dict]:
    """從 PM 的澄清回應抽出 `問題:`／`假設:` 配對（假設行附屬於其上方最近的問題行）。

    `澄清: 不需要` 或全無問題行回空 list（代表需求已足夠明確、不進等待）。
    """
    if re.search(r"^\s*澄清\s*[:：]\s*不需要", text, re.M):
        return []
    out: list[dict] = []
    cur: dict | None = None
    for line in text.splitlines():
        m = re.match(r"^\s*問題\s*[:：]\s*(.+)$", line)
        if m:
            cur = {"q": m.group(1).strip(), "assumption": ""}
            out.append(cur)
            continue
        m = re.match(r"^\s*假設\s*[:：]\s*(.+)$", line)
        if m and cur is not None:
            cur["assumption"] = m.group(1).strip()
    return out


# 可選的「[P0/bug]」標籤：priority（P0~P2）與 type（feature|bug|improvement）皆可省、
# 順序不拘；解析失敗一律退回預設（P1 / improvement），絕不因標籤寫壞丟任務。
_RE_TAGGED_TASK = re.compile(r"^\s*任務\s*[:：]\s*(?:\[([^\]]*)\]\s*)?(.+?)\s*$", re.M)
_RE_TAGGED_FOLLOWUP = re.compile(r"^\s*後續任務\s*[:：]\s*(?:\[([^\]]*)\]\s*)?(.+?)\s*$", re.M)


def _parse_item_tag(tag: str) -> dict:
    """把 `[P0/bug]` 標籤內容解析成 {priority, type}；無法辨識的片段忽略。"""
    priority, item_type = 1, "improvement"
    for part in re.split(r"[/,，\s]+", (tag or "").strip()):
        part = part.strip()
        if part.upper() in ("P0", "P1", "P2"):
            priority = int(part[1])
        elif part.lower() in ("feature", "bug", "improvement"):
            item_type = part.lower()
    return {"priority": priority, "type": item_type}


def parse_structured_tasks(text: str) -> list[dict]:
    """從專家輸出抽出結構化任務（`任務: [P0/bug] <title>`，標籤可省）。

    供「找問題」等回填 backlog 的消費端使用（與 PM 拆解的 parse_tasks 並列、互不影響）。
    完全無 `任務:` 行時退回 parse_tasks 的條列解析（預設 P1/improvement），行為與現狀一致。
    """
    items = [
        {"title": title.strip(), **_parse_item_tag(tag)}
        for tag, title in _RE_TAGGED_TASK.findall(text or "")
        if title.strip()
    ]
    if items:
        return items[: config.MAX_TASKS]
    return [{"title": t, "priority": 1, "type": "improvement"} for t in parse_tasks(text)]


def parse_followups(text: str) -> list[str]:
    """從檢討文字抽出 `後續任務: ...` 行（供 autopilot 回寫 backlog）。

    回傳純標題（剝掉可選的 `[P0/bug]` 標籤）；要保留標籤語意用 parse_followups_meta。
    """
    return [t["title"] for t in parse_followups_meta(text)]


def parse_followups_meta(text: str) -> list[dict]:
    """parse_followups 的結構化版本：每筆 {title, priority, type}（標籤缺省取預設）。"""
    return [
        {"title": title.strip(), **_parse_item_tag(tag)}
        for tag, title in _RE_TAGGED_FOLLOWUP.findall(text or "")
        if title.strip()
    ][:10]


def parse_lessons(text: str) -> list[str]:
    """從檢討文字抽出 `教訓: ...` 行（供跨場次教訓庫累積）。"""
    return [m.strip() for m in re.findall(r"^\s*教訓\s*[:：]\s*(.+)$", text, re.M)][:5]


def parse_vision(text: str) -> str:
    """從澄清/評估文字抽出 `願景: ...`（一句產品願景，回填專案 meta 用）；無標記回空字串。"""
    return _last_match(text, r"願景\s*[:：]\s*(.+)") or ""


# --- 議程解析（子題＋負責分派） -------------------------------------------

# 解析端硬上限：prompt 的「2–5 個」只是建議不是防線，超出一律截斷並 log。
MAX_AGENDA_ITEMS = 5


def parse_agenda(text: str, requirement: str = "") -> list[dict]:
    """從拆解文字抽出議程子題列表，每筆 {title, description, criteria, assignee}。

    子題行：`子題: <標題> | <描述> | <成功準則>`——用 `split("|", 2)` 固定最多切三段，
    多餘的 `|` 全部歸入成功準則（標題/描述不會被錯切）；缺段允許為空字串。
    負責行：`負責: <role_key>` 附屬於其上方最近的子題行；找不到前置子題時忽略＋log。
    無任何 `子題:` 行時 fallback 為單一子題（原需求全文 requirement，缺則用 text），
    不噴錯——探索型議題允許不硬拆。子題數超過 MAX_AGENDA_ITEMS 截斷並 log。
    assignee 為原始字串、未經驗證；合法性交由 validate_assignees 硬驗證。
    """
    items: list[dict] = []
    cur: dict | None = None
    truncated = 0
    for line in (text or "").splitlines():
        m = re.match(r"^\s*子題\s*[:：]\s*(.+)$", line)
        if m:
            if len(items) >= MAX_AGENDA_ITEMS:
                truncated += 1
                cur = None  # 被截斷子題的後續 `負責:` 一併忽略。
                continue
            parts = [p.strip() for p in m.group(1).split("|", 2)]
            parts += [""] * (3 - len(parts))
            cur = {
                "title": parts[0],
                "description": parts[1],
                "criteria": parts[2],
                "assignee": "",
            }
            items.append(cur)
            continue
        m = re.match(r"^\s*負責\s*[:：]\s*(\S+)\s*$", line)
        if m:
            if cur is None:
                log.warning("議程解析：`負責: %s` 找不到前置子題行，忽略", m.group(1))
            else:
                cur["assignee"] = m.group(1)
    if truncated:
        log.warning("議程解析：子題數超過上限 %d，截斷 %d 筆", MAX_AGENDA_ITEMS, truncated)
    if items:
        return items
    fallback_title = (requirement or text or "").strip() or "實作需求"
    return [{"title": fallback_title, "description": "", "criteria": "", "assignee": ""}]


def validate_assignees(
    agenda: list[dict],
    available_keys,
    fallback: str = "engineer",
) -> tuple[list[dict], list[dict]]:
    """硬驗證議程分派：assignee 必須屬於本場實際出席角色集合 available_keys。

    非法或缺漏時 fallback 順序：`fallback`（預設 engineer）若在出席集合，否則取
    第一個出席者——純函式不依賴呼叫端保證 engineer 一定出席。available_keys 為空
    時不修正（assignee 清空）、只記 log，不丟例外。

    回傳 (新議程列表, 修正紀錄)；修正紀錄每筆 {index, given, assigned}，供呼叫端
    記 log 與議程事件。輸入 agenda 不被就地修改。
    """
    keys = list(dict.fromkeys(available_keys or []))  # 去重、保序。
    effective = fallback if fallback in keys else (keys[0] if keys else "")
    out: list[dict] = []
    corrections: list[dict] = []
    for i, item in enumerate(agenda):
        given = (item.get("assignee") or "").strip()
        if given in keys:
            out.append({**item, "assignee": given})
            continue
        out.append({**item, "assignee": effective})
        corrections.append({"index": i, "given": given, "assigned": effective})
        if not keys:
            log.warning("議程分派：無可用角色集合，子題 #%d 的 `負責: %s` 清空", i, given)
        else:
            log.warning(
                "議程分派：子題 #%d 的 `負責: %s` 非法或缺漏，fallback 至 %s",
                i, given or "(缺)", effective,
            )
    return out, corrections


def parse_tasks_with_deps(pm_text: str) -> tuple[list[dict], list[tuple[int, int]]]:
    """從 PM 拆解文字抽出任務（含可選 `#id`）與依賴邊，供並行分波使用。

    任務行：`任務: [#<id>] <title>`（`#id` 可選，缺則依出現序自動編號，1-based）。
    依賴行：`依賴: #<after> -> #<before>`（after 須在 before 完成後才做）。
    無顯式 `任務:` 行時退回 `parse_tasks` 的條列解析（自動編號、無依賴），與循序行為一致。
    指向不存在任務 id 的依賴邊一律丟棄（防懸空）。任務數沿用 `MAX_TASKS` 上限。
    """
    cap = config.MAX_TASKS
    tasks: list[dict] = []
    explicit = re.findall(r"^\s*任務\s*[:：]\s*(?:#(\d+)\s+)?(.+?)\s*$", pm_text, re.M)
    if explicit:
        used: set[int] = set()
        for pos, (rid, title) in enumerate(explicit[:cap], start=1):
            tid = int(rid) if rid else pos
            while tid in used:  # 顯式 id 與自動序衝突時往後讓位，保證 id 唯一。
                tid = max(used) + 1
            used.add(tid)
            tasks.append({"id": tid, "title": title.strip(), "status": "todo"})
    else:
        for pos, title in enumerate(parse_tasks(pm_text)[:cap], start=1):
            tasks.append({"id": pos, "title": title, "status": "todo"})

    valid_ids = {t["id"] for t in tasks}
    edges: list[tuple[int, int]] = []
    for after, before in re.findall(r"^\s*依賴\s*[:：]\s*#(\d+)\s*->\s*#(\d+)\s*$", pm_text, re.M):
        a, b = int(after), int(before)
        if a in valid_ids and b in valid_ids and a != b:
            edges.append((a, b))
    return tasks, edges


def build_waves(tasks: list[dict], edges: list[tuple[int, int]]) -> list[list[dict]]:
    """依依賴邊把任務拓撲分層成「波次」：同一波內任務彼此獨立、可並行；波次之間循序。

    邊 (after, before) 表示 after 須在 before 完成後才做。以 Kahn 演算法逐層取出入度 0 的
    任務（穩定按 id 排序，結果可重現）。偵測到循環依賴時，剩餘任務退回「每任務一波」的純
    循序 fallback，確保永遠有解、不卡死。指向未知 id 的邊忽略（防懸空）。
    """
    by_id = {t["id"]: t for t in tasks}
    indeg = {tid: 0 for tid in by_id}
    adj: dict[int, list[int]] = {tid: [] for tid in by_id}
    for after, before in edges:
        if after in by_id and before in by_id and after != before:
            adj[before].append(after)
            indeg[after] += 1

    waves: list[list[dict]] = []
    remaining = set(by_id)
    while remaining:
        layer = sorted(tid for tid in remaining if indeg[tid] == 0)
        if not layer:
            # 循環依賴：剩餘任務退回每任務一波（按 id 序），保證收斂、不靜默卡死。
            for tid in sorted(remaining):
                waves.append([by_id[tid]])
            break
        waves.append([by_id[tid] for tid in layer])
        for tid in layer:
            remaining.discard(tid)
            for nxt in adj[tid]:
                indeg[nxt] -= 1
    return waves
