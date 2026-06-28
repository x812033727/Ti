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

from . import config, provider_quota

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


def shippable_verdict(*, all_ok: bool, demo_veto: bool, core_verified: bool, stopped: bool) -> bool:
    """是否可出貨（可帶已知限制）——把「全有全無」放寬為「核心客觀證據通過即出貨」。

    - stopped：被中止一律不出貨。
    - demo_veto：最終 Demo／整合「實際跑過且失敗」＝客觀失敗,硬擋,不出貨。
    - all_ok：所有任務都通過 → 完整出貨（原行為）。
    - core_verified：最終 Demo／整合「實際跑過且通過」＝核心客觀證據,縱使有次要任務未過,
      仍以「已知限制」版本出貨（未過任務記進交付物＋留 backlog）。
    安全護欄:既非 all_ok、又無 core_verified（沒跑過 Demo、無任何客觀證據）時不出貨,
    避免把未經驗證的半成品推出去。
    """
    if stopped or demo_veto:
        return False
    return all_ok or core_verified


def plan_preflight_rebind(
    current_bindings: dict[str, str],
    snapshot: dict | None,
    explicit_overrides: dict[str, str],
) -> list[tuple[str, str, str]]:
    """規劃場次起點 provider 重綁；純判定，不碰 Role/config/expert 物件。

    回傳 (role_key, from_provider, to_provider)。全受限時沒有可用 to_provider，故不產生
    plan；呼叫端可依 constrained 另做事件/audit。
    """
    if not snapshot:
        return []
    alt = provider_quota.least_constrained_ready(snapshot)
    if not alt:
        return []
    plan: list[tuple[str, str, str]] = []
    for role_key, provider in current_bindings.items():
        provider = (provider or "").strip()
        if not provider or explicit_overrides.get(role_key):
            continue
        if provider == alt:
            continue
        if provider_quota.constrained(snapshot, provider):
            plan.append((role_key, provider, alt))
    return plan


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
_RE_CORE_CHANGE = re.compile(r"^\s*核心改動\s*[:：]\s*(?:\[([^\]]*)\]\s*)?(.+?)\s*$", re.M)


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


def classify_failure_followups(failed_titles: list[str], retro_items: list[dict]) -> list[dict]:
    """把客觀失敗來源升格為 P0 bug，未命中的檢討後續任務維持原標籤。

    failed_titles 來自 Demo/QA 等機器可判定失敗；retro_items 則是 parse_followups_meta
    已解析出的檢討後續任務。兩者同標題時以失敗事實為準，覆寫成 P0/bug 且不重複產生項目。
    """
    failed: list[str] = []
    failed_set: set[str] = set()
    for title in failed_titles or []:
        clean = str(title or "").strip()
        if clean and clean not in failed_set:
            failed.append(clean)
            failed_set.add(clean)

    items: list[dict] = []
    seen: set[str] = set()
    for item in retro_items or []:
        title = str((item or {}).get("title", "")).strip()
        if not title or title in seen:
            continue
        out = {**item, "title": title}
        if title in failed_set:
            out["priority"] = 0
            out["type"] = "bug"
        items.append(out)
        seen.add(title)

    for title in failed:
        if title not in seen:
            items.append({"title": title, "priority": 0, "type": "bug"})
            seen.add(title)
    return items


def parse_core_changes(text: str) -> list[dict]:
    """從專家輸出抽出 `核心改動: [P0/bug] <說明>` 行——代表「要滿足本專案需求，必須改動 Ti 核心
    框架本身（orchestrator／runner／發佈流程等），而非專案自己的程式碼」。

    回傳結構化任務 {title, priority, type}（與 parse_followups_meta 同形），供路由到核心 backlog
    （config.CORE_REPO＝x812033727/Ti），由 autopilot 在核心 repo 的 working clone 實作、過閘門、
    開「獨立 PR」——絕不混入專案 repo。標籤缺省取預設（P1／improvement）。
    """
    return [
        {"title": title.strip(), **_parse_item_tag(tag)}
        for tag, title in _RE_CORE_CHANGE.findall(text or "")
        if title.strip()
    ][:10]


def parse_lessons(text: str) -> list[str]:
    """從檢討文字抽出 `教訓: ...` 行（供跨場次教訓庫累積）。"""
    return [m.strip() for m in re.findall(r"^\s*教訓\s*[:：]\s*(.+)$", text, re.M)][:5]


def parse_vision(text: str) -> str:
    """從澄清/評估文字抽出 `願景: ...`（一句產品願景，回填專案 meta 用）；無標記回空字串。"""
    return _last_match(text, r"願景\s*[:：]\s*(.+)") or ""


# --- 結論彙整解析（共識／分歧／未決／行動） -------------------------------

# 四前綴 → 結構化鍵；順序即輸出 dict 鍵順序。
_CONCLUSION_PREFIXES = (
    ("共識", "consensus"),
    ("分歧", "disagreements"),
    ("未決", "open_questions"),
    ("行動", "actions"),
)


def parse_conclusion(text: str) -> dict[str, list[str]]:
    """解析 senior 蒸餾輸出的四段行前綴 `共識:／分歧:／未決:／行動:`，回傳結構化 dict。

    沿用既有行前綴 parser 範式（`^\\s*<標籤>\\s*[:：]...(.+)$` ＋全形冒號容錯）；每個
    前綴可出現多行，逐行收進對應 list（剝去前後空白、跳過空內容）。

    注意：冒號後的水平空白用 `[^\\S\\n]*`（不含換行）而非 `\\s*`——`\\s` 含 `\\n`，若空
    內容前綴行（如 LLM 只輸出 `共識:`）用 `\\s*` 會吃掉換行、把下一行整行吞入並錯分類，
    跨前綴污染（全形空白 `\\u3000` 仍被 `[^\\S\\n]` 接受，容錯不受影響）。

    四前綴全缺時回空骨架（四鍵皆空 list）而非拋例外，由呼叫端偵測空骨架走 fallback，
    對齊 `adr.parse_adr` 「失敗即降級」。
    """
    result: dict[str, list[str]] = {key: [] for _, key in _CONCLUSION_PREFIXES}
    for label, key in _CONCLUSION_PREFIXES:
        for m in re.findall(rf"^[^\S\n]*{label}[^\S\n]*[:：][^\S\n]*(.+)$", text or "", re.M):
            item = m.strip()
            if item:
                result[key].append(item)
    return result


# --- 議程解析（子題＋負責分派） -------------------------------------------

# 解析端硬上限：prompt 的「2–5 個」只是建議不是防線，超出一律截斷並 log。
MAX_AGENDA_ITEMS = 5


def parse_agenda(text: str, requirement: str = "") -> list[dict]:
    """從拆解文字抽出議程子題列表，每筆 {title, description, criteria, assignee}。

    子題行：`子題: <標題> | <描述> | <成功準則>`——用 `split("|", 2)` 固定最多切三段，
    多餘的 `|` 全部歸入成功準則（標題/描述不會被錯切）；全形 `｜` 先正規化為半形再切
    （LLM 輸出常混用）；缺段允許為空字串。標題空、描述非空時以描述補位（log）；全段
    皆空的子題行整行跳過（log），不產出空殼子題。
    負責行：`負責: <role_key>` 附屬於其上方最近的子題行；找不到前置子題時忽略＋log；
    key 後帶多餘文字（如 `負責: engineer (主寫)`）不符單一 token 規格——不採信、
    記 warning（不靜默吞行），留待 validate_assignees fallback 兜底。
    無任何 `子題:` 行時 fallback 為單一子題（原需求全文 requirement，缺則用 text），
    不噴錯——探索型議題允許不硬拆。子題數超過 MAX_AGENDA_ITEMS 截斷並 log。
    assignee 為原始字串、未經驗證；合法性交由 validate_assignees 硬驗證。

    新 API、不入 orchestrator re-export：消費端一律 `from studio.flow import`。
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
            body = m.group(1).replace("｜", "|")  # 全形管線正規化，避免整行誤入 title。
            parts = [p.strip() for p in body.split("|", 2)]
            parts += [""] * (3 - len(parts))
            if not parts[0]:
                if not parts[1] and not parts[2]:
                    log.warning("議程解析：子題行全段皆空，整行跳過: %r", line.strip())
                    cur = None  # 後續 `負責:` 不得附到上一個子題。
                    continue
                log.warning("議程解析：子題標題為空，以描述補位: %r", line.strip())
                if parts[1]:
                    parts[0], parts[1] = parts[1], ""
                else:
                    parts[0], parts[2] = parts[2], ""
            cur = {
                "title": parts[0],
                "description": parts[1],
                "criteria": parts[2],
                "assignee": "",
            }
            items.append(cur)
            continue
        m = re.match(r"^\s*負責\s*[:：]\s*(.+?)\s*$", line)
        if m:
            tokens = m.group(1).split()
            if cur is None:
                log.warning("議程解析：`負責: %s` 找不到前置子題行，忽略", m.group(1))
                continue
            if len(tokens) != 1:
                log.warning(
                    "議程解析：`負責: %s` 不符單一 token 規格，不採信（交 validate 兜底）",
                    m.group(1),
                )
                continue
            cur["assignee"] = tokens[0]
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
                i,
                given or "(缺)",
                effective,
            )
    return out, corrections


# --- 動態 step：PM 運行時決定下一步（dynamic workflow stage 用）----------

# 結束 token（大小寫不敏感）：PM 宣告動態流程收斂時用。
_NEXT_STEP_END = {"結束", "結束。", "完成", "停止", "end", "done", "stop", "finish"}


def parse_next_step(text: str) -> dict:
    """從 PM 的動態決策輸出解析下一步，回傳 ``{role, instruction, end, recruit, provider}``。

    格式（沿用本檔行前綴 parser 範式，全形冒號容錯）：
    - ``下一步: <role_key>`` —— 下一個發言角色（取最後一個 `下一步:` 行為準）。
    - ``下一步: 結束``（或 完成／停止／end／done／stop／finish，大小寫不敏感）→ end=True、role 清空。
    - ``指示: <要該角色做什麼>`` —— 選填，附給被選角色的指示（取最後一行）。
    - ``招募: <key> | <名稱> | <一句專長>`` —— 選填，PM 現場液生一個新 persona（取最後一行）；
      呼叫端可據此建臨時角色加入（key 不合法/缺專長則忽略）。
    - ``provider: <claude|codex|minimax|antigravity>`` —— 選填，招募時指定綁哪個 provider（取最後一行）。

    role/recruit/provider 皆為原始字串、**未驗證**：合法性與 fallback 交由呼叫端
    （validate_assignees／KEY_RE／provider 白名單）兜底。找不到任何 `下一步:` 行 → role 空、
    end False（呼叫端據此走 fallback 或結束）。

    新 API、不入 orchestrator re-export：消費端一律 ``from studio.flow import``。
    """
    role, instruction, end = "", "", False
    recruit: dict | None = None
    provider = ""
    for line in (text or "").splitlines():
        m = re.match(r"^\s*下一步\s*[:：]\s*(.+?)\s*$", line)
        if m:
            val = m.group(1).strip()
            if val.lower() in _NEXT_STEP_END or val in _NEXT_STEP_END:
                role, end = "", True
            else:
                tokens = val.split()
                role, end = (tokens[0] if tokens else ""), False
            continue
        m = re.match(r"^\s*指示\s*[:：]\s*(.+?)\s*$", line)
        if m:
            instruction = m.group(1).strip()
            continue
        m = re.match(r"^\s*provider\s*[:：]\s*(.+?)\s*$", line, re.I)
        if m:
            provider = m.group(1).strip().split()[0].lower() if m.group(1).strip() else ""
            continue
        m = re.match(r"^\s*招募\s*[:：]\s*(.+)$", line)
        if m:
            parts = [p.strip() for p in m.group(1).replace("｜", "|").split("|", 2)]
            parts += [""] * (3 - len(parts))
            if parts[0]:
                recruit = {"key": parts[0], "name": parts[1], "expertise": parts[2]}
    return {
        "role": role,
        "instruction": instruction,
        "end": end,
        "recruit": recruit,
        "provider": provider,
    }


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
