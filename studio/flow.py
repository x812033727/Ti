"""討論流程的純函式層：決議解析、停滯偵測、任務／依賴／教訓解析與波次規劃。

無狀態、不依賴 StudioSession，自 orchestrator.py 抽出（行為逐字不變）。orchestrator.py
以顯式 re-export 保住既有 import 路徑——tests、autopilot、improver 皆 `from studio.orchestrator
import ...`，且對 `studio.orchestrator.<fn>` 的 monkeypatch 仍有效（orchestrator 內部沿用裸名
查找本模組屬性）。對 `studio.flow.<fn>` 的 monkeypatch 不影響 orchestrator 內部呼叫。

額度感知 per-task 派工的新 marker（與既有 `任務:`／`依賴:`／`下一步:` 同為穩定字串，改動前
先確認對應 parser）：
- ``派工: #<id> <provider> [<model>]`` —— PM 拆解時對單一任務的派工建議（parse_dispatch），
  合法性與額度受限由 choose_dispatch 對照 digest／allowed_models 兜底。
- ``模型: <model>`` —— 動態 step 招募時指定綁定 provider 的模型（parse_next_step 的 model 鍵）。
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


def parse_help_request(text: str) -> str:
    """從工程師發言抽出 `求助: <一句問題>`（實作中途卡關即時求助 PM 用）；無標記回空字串。"""
    return _last_match(text, r"求助\s*[:：]\s*(.+)") or ""


def parse_workflow_choice(text: str) -> str:
    """從 PM 分診輸出抽出 `流程: <名稱>`（autopilot 開場 workflow 分診用）；無標記回空字串。

    只負責抽字串，不驗名稱合法性——白名單（限內建流程）是呼叫端 autopilot
    `_select_workflow` 的職責，解析層不做政策判斷。
    """
    return _last_match(text, r"流程\s*[:：]\s*(.+)") or ""


def parse_triage_reason(text: str) -> str:
    """從 PM 分診輸出抽出 `理由: <一句話>`（workflow 分診的稽核註記用）；無標記回空字串。"""
    return _last_match(text, r"理由\s*[:：]\s*(.+)") or ""


def parse_incomplete_reason(text: str) -> str:
    """從 PM 驗收輸出抽出 `原因: <一句根因>`（判「未完成」時的裁決原因）；無標記回空字串。

    讓 autopilot 的「討論未達完成」失敗 note 帶上結構化根因（供 triage 分診與人工回看），
    而非只有一句無資訊量的「未收斂」。只抽字串，不做語意判斷。
    """
    return _last_match(text, r"原因\s*[:：]\s*(.+)") or ""


# 調查輸出的區塊終止標記：`結論:` 之後遇到任一這些行前綴即視為結論段結束。
_INVESTIGATION_STOP_RE = re.compile(r"^\s*(證據|後續任務|需人工|需改碼|教訓|任務)\s*[:：]")


def parse_investigation(text: str) -> dict:
    """解析單專家「調查/驗證」任務的結構化輸出（autopilot 調查分流輕量管線用）。

    回傳 dict：
      - conclusion：`結論:` 起、至下一個已知行前綴（證據/後續任務/需人工/需改碼/教訓/任務）
        或文末的多行結論全文（strip 後）；無標記＝空字串（代表調查失敗，呼叫端走重試）。
      - evidence：所有 `證據:` 行內容（list，保序）。
      - needs_human：`需人工: <原因>`（AI 做不到、須人工處理）；無＝空字串。
      - needs_code：`需改碼: <原因>`（判定須實際改碼才算完成，應升級回完整管線）；無＝空字串。
      - followups：`後續任務:` 行（沿用 parse_followups_meta，含 priority/type 標籤）。

    純字串解析、stdlib-only；政策（done/parked/升級）由呼叫端 autopilot 判斷。
    """
    lines = text.splitlines()
    conclusion_parts: list[str] = []
    collecting = False
    for line in lines:
        m = re.match(r"^\s*結論\s*[:：]\s*(.*)$", line)
        if m:
            # 以最後一個 `結論:` 為準（與 _last_match 的「取最後」慣例一致）
            conclusion_parts = [m.group(1).strip()]
            collecting = True
            continue
        if collecting:
            if _INVESTIGATION_STOP_RE.match(line):
                collecting = False
                continue
            conclusion_parts.append(line.rstrip())
    conclusion = "\n".join(conclusion_parts).strip()
    return {
        "conclusion": conclusion,
        "evidence": [
            m.strip() for m in re.findall(r"^\s*證據\s*[:：]\s*(.+)$", text, re.M) if m.strip()
        ],
        "needs_human": _last_match(text, r"需人工\s*[:：]\s*(.+)") or "",
        "needs_code": _last_match(text, r"需改碼\s*[:：]\s*(.+)") or "",
        "followups": parse_followups_meta(text),
    }


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
    """從 PM 的動態決策輸出解析下一步，回傳 ``{role, instruction, end, recruit, provider, model}``。

    格式（沿用本檔行前綴 parser 範式，全形冒號容錯）：
    - ``下一步: <role_key>`` —— 下一個發言角色（取最後一個 `下一步:` 行為準）。
    - ``下一步: 結束``（或 完成／停止／end／done／stop／finish，大小寫不敏感）→ end=True、role 清空。
    - ``指示: <要該角色做什麼>`` —— 選填，附給被選角色的指示（取最後一行）。
    - ``招募: <key> | <名稱> | <一句專長>`` —— 選填，PM 現場液生一個新 persona（取最後一行）；
      呼叫端可據此建臨時角色加入（key 不合法/缺專長則忽略）。
    - ``provider: <claude|codex|minimax|antigravity>`` —— 選填，招募時指定綁哪個 provider（取最後一行）。
    - ``模型: <model>`` —— 選填，招募時指定該 provider 的模型（取最後一行；可含空白）。

    role/recruit/provider/model 皆為原始字串、**未驗證**：合法性與 fallback 交由呼叫端
    （validate_assignees／KEY_RE／provider 白名單／模型白名單）兜底。找不到任何 `下一步:` 行
    → role 空、end False（呼叫端據此走 fallback 或結束）。

    新 API、不入 orchestrator re-export：消費端一律 ``from studio.flow import``。
    """
    role, instruction, end = "", "", False
    recruit: dict | None = None
    provider = ""
    model = ""
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
        m = re.match(r"^\s*模型\s*[:：]\s*(.+?)\s*$", line)
        if m:
            model = m.group(1).strip()
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
        "model": model,
    }


# --- 額度感知 per-task 派工（純函式決策；快照查詢與換綁副作用在 orchestrator）--------


def parse_dispatch(text: str) -> dict[int, dict]:
    """解析 PM 拆解輸出的派工行，回傳 ``{task_id: {"provider": ..., "model": ...}}``。

    行格式：``派工: #<id> <provider> [<model>]``（沿用本檔 marker 範式：行前綴、全形冒號
    容錯、逐行收集；同一 id 出現多行取最後一行）。model 可含空白（如 Antigravity 的顯示
    名稱），省略＝空字串。provider 正規化為小寫；兩者皆**未驗證**——合法性由
    choose_dispatch 對照 digest 與 allowed_models 兜底。無任何派工行回空 dict。

    新 API、不入 orchestrator re-export：消費端一律 ``from studio.flow import``。
    """
    out: dict[int, dict] = {}
    for line in (text or "").splitlines():
        m = re.match(r"^\s*派工\s*[:：]\s*#(\d+)\s+(\S+)(?:\s+(.+?))?\s*$", line)
        if m:
            out[int(m.group(1))] = {
                "provider": m.group(2).strip().lower(),
                "model": (m.group(3) or "").strip(),
            }
    return out


def choose_dispatch(
    digest: dict,
    task: dict,
    hint: dict,
    allowed_models: dict,
    recent: list[str],
    performance: dict | None = None,
    threshold: float = 90.0,
    model_free: bool = False,
) -> dict:
    """依即時額度為單一任務挑 provider／model，回傳 ``{provider, model, reason}``。

    純函式：額度快照的查詢（provider_quota.digest）、換綁專家與事件廣播等副作用都在
    orchestrator；本函式只做決策，可單元測試、可 monkeypatch。

    參數：
    - digest: ``{provider: {ready, error, max_used, soonest_reset}}``。
    - task: 任務 dict（``{id, title, ...}``，供 reason 描述）。
    - hint: parse_dispatch 對該任務的派工建議（``{provider, model}``）或 ``{}``。
    - allowed_models: ``{provider: tuple[str, ...]}`` 模型白名單；hint.model 不在名單即棄用。
    - recent: 已派 provider 序列（尾端＝最近），同分時避開剛用過的、把任務分攤到各家。
    - performance: 可選 ``{provider: avg_score}``——同用量時偏好歷史表現高者（缺省視為 0）。
      目前僅作次序鍵；詳細考核資料流由後續 PR 接上。
    - threshold: 受限門檻（任一額度窗用量 % 達此值即視為受限）。
    - model_free: auto 派工模式——hint.model 不查白名單、原樣直通；但僅限「選定 provider
      ＝hint 的 provider」時（被兜底改派到另一家時模型丟空，改用該家預設槽，避免 A 家的
      模型 ID 直通 B 家必炸）。False＝現行白名單行為，一字不變。

    規則：hint.provider 合法（在 digest 內）且未受限（就緒、無 error、用量<門檻）→ 採用；
    否則在「就緒未受限」集合取用量最低者（同分先比 performance 高者、再避開 recent 尾端剛
    用過的、最後以 digest 次序決勝確保可重現）；全受限時取「就緒」中用量最低者；全掛回
    ``{"provider": "", "model": "", "reason": ...}``（呼叫端沿用原綁定）。

    model：hint.model 在 allowed_models[選定 provider] 內才採用，否則空字串（沿用該
    provider 的預設模型槽）。reason 為繁中一句話（供 dispatch_decision 事件顯示）。
    """
    perf = performance or {}
    hint = hint or {}

    def _usage(key: str) -> dict:
        return digest.get(key) or {}

    def _used(key: str) -> float:
        used = _usage(key).get("max_used")
        return float(used) if used is not None else 0.0

    def _ready(key: str) -> bool:
        u = _usage(key)
        return bool(u.get("ready")) and not u.get("error")

    def _unconstrained(key: str) -> bool:
        return _ready(key) and _used(key) < threshold

    def _model_for(provider: str) -> str:
        model = (hint.get("model") or "").strip()
        if model_free:
            hinted_prov = (hint.get("provider") or "").strip().lower()
            return model if model and provider == hinted_prov else ""
        return model if model and model in (allowed_models.get(provider) or ()) else ""

    tid = task.get("id", "?")
    hinted = ((hint.get("provider") or "").strip().lower()) if hint else ""
    if hinted and hinted in digest and _unconstrained(hinted):
        return {
            "provider": hinted,
            "model": _model_for(hinted),
            "reason": f"任務 #{tid}：PM 指定 {hinted}，額度未受限，照派",
        }

    order = {key: i for i, key in enumerate(digest)}  # digest 保序：最終同分以此決勝（可重現）
    last = recent[-1] if recent else ""
    candidates = [k for k in digest if _unconstrained(k)]
    if candidates:
        best = min(
            candidates,
            key=lambda k: (_used(k), -float(perf.get(k) or 0.0), 1 if k == last else 0, order[k]),
        )
        prefix = f"PM 指定的 {hinted} 受限或不可用，" if hinted else ""
        return {
            "provider": best,
            "model": _model_for(best),
            "reason": f"任務 #{tid}：{prefix}改派就緒中用量最低的 {best}（{_used(best):.0f}%）",
        }
    ready = [k for k in digest if _ready(k)]
    if ready:
        best = min(ready, key=lambda k: (_used(k), order[k]))
        return {
            "provider": best,
            "model": _model_for(best),
            "reason": f"任務 #{tid}：各 provider 額度皆受限，取用量最低的 {best}（{_used(best):.0f}%）",
        }
    return {"provider": "", "model": "", "reason": f"任務 #{tid}：無任何就緒 provider，沿用原綁定"}


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


# --- 3-AI 表決（PM 無法決定時跨 provider 多數決；副作用在 orchestrator._hold_vote）--------


def parse_vote_request(text: str) -> dict | None:
    """解析 PM 的表決請求行 ``表決: <議題> | <選項A> | <選項B>[ | <選項C>]``。

    沿用本檔 marker 範式：行前綴＋全形冒號容錯、全形管線 ``｜`` 先正規化為半形再切、
    取最後一個命中行為準（與 ``_last_match`` 一致）。選項剔除空段落、去重保序。
    無命中行、議題為空、或有效選項不足 2 個 → 回 None（不是合法表決請求）。
    合法時回 ``{"topic": <議題>, "options": [<選項>...]}``。

    新 API、不入 orchestrator re-export：消費端一律 ``from studio.flow import``。
    """
    matches = re.findall(r"^\s*表決\s*[:：]\s*(.+?)\s*$", text or "", re.M)
    if not matches:
        return None
    parts = [p.strip() for p in matches[-1].replace("｜", "|").split("|")]
    topic = parts[0]
    options = list(dict.fromkeys(p for p in parts[1:] if p))  # 去空段、去重、保序
    if not topic or len(options) < 2:
        return None
    return {"topic": topic, "options": options}


def parse_ballot(text: str, options: list[str]) -> str:
    """解析投票員輸出的 ``投票: <選項>`` 行，正規化回 options 中的選項原文。

    取最後一個命中行（全形冒號容錯）。精確比對優先；否則以 difflib 相似度對每個
    選項打分、≥0.6 取最佳者（LLM 常少字/多字/改標點，不因此丟票）。無命中行、
    options 為空、或與所有選項都不像 → 回 ""（棄權）。
    """
    matches = re.findall(r"^\s*投票\s*[:：]\s*(.+?)\s*$", text or "", re.M)
    if not matches or not options:
        return ""
    val = matches[-1]
    if val in options:
        return val
    best, best_ratio = "", 0.0
    for opt in options:
        ratio = difflib.SequenceMatcher(None, val, opt).ratio()
        if ratio > best_ratio:
            best, best_ratio = opt, ratio
    return best if best_ratio >= 0.6 else ""


def tally_votes(ballots: list[dict]) -> dict:
    """多數決計票。ballots＝``[{voter, provider, choice}]``；choice 空字串＝棄權（不計票）。

    回 ``{"winner": <選項>, "counts": {選項: 票數}, "tie": bool}``：
    - 唯一最高票 → 該選項為 winner、tie=False。
    - 最高票平手 → tie=True；PM（voter=="pm"）有投且其票在平手集合 → 以 PM 票為
      winner（僵局時 PM 票定案），否則 winner=""（交呼叫端降級兜底）。
    - 全棄權／空 ballots → ``{"winner": "", "counts": {}, "tie": False}``。
    """
    counts: dict[str, int] = {}
    for b in ballots or []:
        choice = (b or {}).get("choice") or ""
        if choice:
            counts[choice] = counts.get(choice, 0) + 1
    if not counts:
        return {"winner": "", "counts": {}, "tie": False}
    top = max(counts.values())
    leaders = [c for c, n in counts.items() if n == top]
    if len(leaders) == 1:
        return {"winner": leaders[0], "counts": counts, "tie": False}
    pm_choice = next(
        (b.get("choice") for b in ballots if (b or {}).get("voter") == "pm" and b.get("choice")),
        "",
    )
    winner = pm_choice if pm_choice in leaders else ""
    return {"winner": winner, "counts": counts, "tie": True}


def pick_vote_providers(
    digest: dict, exclude: str, n: int = 2, threshold: float = 90.0
) -> list[str]:
    """從額度 digest 挑至多 n 個可當投票員的 provider，回 provider key 列表。

    digest＝``provider_quota.digest`` 的 plain dict（``{provider: {ready, error,
    max_used, soonest_reset}}``）——flow 不 import provider_quota，由 orchestrator
    查快照後把 digest 當參數傳入（與 choose_dispatch 同一邊界）。

    入選條件：就緒（ready）、無 error、``max_used`` 未達 threshold（缺用量資訊視為
    0＝最寬鬆）、且 ≠ exclude（PM 自己的 provider，表決須跨 provider）。按 max_used
    升冪取前 n（同分按 digest 次序決勝、可重現）；彼此天然相異（digest 鍵唯一）。
    合格者不足 n 個時回實際數（呼叫端據此降級）。
    """
    exclude = (exclude or "").strip().lower()
    candidates: list[tuple[float, int, str]] = []
    for i, (key, usage) in enumerate((digest or {}).items()):
        u = usage or {}
        if key == exclude or not u.get("ready") or u.get("error"):
            continue
        used = float(u["max_used"]) if u.get("max_used") is not None else 0.0
        if used >= threshold:
            continue
        candidates.append((used, i, key))
    candidates.sort()
    return [key for _, _, key in candidates[: max(n, 0)]]


# --- 考核（Appraisal）解析（純函式；持久化在 studio/appraisal.py、事件廣播在 orchestrator）---

# 全形數字容錯：LLM 偶爾輸出「４分」這類全形分數，translate 正規化後再驗證範圍。
_FULLWIDTH_DIGITS = str.maketrans("０１２３４５６７８９", "0123456789")


def parse_appraisals(text: str) -> list[dict]:
    """解析 PM 收尾檢討的考核行，回傳 ``[{"target", "score", "comment"}, ...]``。

    行格式：``考核: <角色或provider> <1-5> <一句評語>``（沿用本檔 marker 範式：行前綴、
    全形冒號容錯、逐行收集）。分數容錯「分」字尾與全形數字；非 1–5 **整數**（0、6、4.5、
    非數字…）一律丟棄該行——絕不讓 LLM 亂給的分數直通長期庫。target 正規化為小寫
    （provider 名／role key 皆小寫慣例）；評語可空。無任何考核行回空 list。

    新 API、不入 orchestrator re-export：消費端一律 ``from studio.flow import``。
    """
    out: list[dict] = []
    for line in (text or "").splitlines():
        m = re.match(
            r"^\s*考核\s*[:：]\s*(\S+)\s+([0-9０-９]+(?:[.．][0-9０-９]+)?)\s*分?\s*(.*?)\s*$",
            line,
        )
        if not m:
            continue
        raw = m.group(2).translate(_FULLWIDTH_DIGITS).replace("．", ".")
        try:
            score = float(raw)
        except ValueError:  # 防禦性：正則已限數字形，理論上不會進來
            continue
        if not score.is_integer() or not 1 <= score <= 5:
            continue
        out.append(
            {
                "target": m.group(1).strip().lower(),
                "score": int(score),
                "comment": m.group(3).strip(),
            }
        )
    return out
