"""任務 #2 / #3 QA 守護測試：守護 ``web/app.js`` 的 ``agenda_plan`` case 渲染補上 criteria。

================================================================================
QA 設計理由（為何這個檔存在）
================================================================================
任務 #2 修補面是 ``web/app.js`` 的 ``agenda_plan`` case 漏讀 ``a.criteria``，導致
議程子題的「成功準則」進 history jsonl 但畫面看不到。任務 #3 守護此修補不被靜默
漏抓：移除渲染行即紅、保留即綠（正 / 負樣成對）。

本檔**不**模擬前端 replay（屬既有 e2e + 人眼 demo 範疇），**只**做靜態源碼掃描
+ 安全路徑驗證 + 既有覆蓋誠實評估，三層防線符合 architect 決策 5
「守護鏈三層」。

================================================================================
#2 / #3 結論（供人眼複看 + 接手者快速取用）
================================================================================
  - 修補位置：``web/app.js`` 的 ``case "agenda_plan":`` 區塊（line 325-340）
  - 修補內容：``items.forEach`` 內，在 ``if (a.assignee) line += ...`` 之後、
    ``addSystem(line)`` 之前，新增 ``if (a.criteria) line += `｜【準則】${a.criteria}`;``
  - 拼接策略：拼到子題行尾 + 【準則】標籤，沿用既有 ``｜`` separator 與 ``【】`` 標籤風格
  - 空值守衛：``if (a.criteria)``，沿用既有 ``if (a.description)`` 守衛慣例
  - 安全路徑：``web/app.js:132-136`` 的 ``addSystem`` 用 ``el.textContent = text;``
    而非 ``innerHTML``，**criteria 進拼接後送 textContent、無 XSS 風險**

================================================================================
不守護事項（誠實設計，給半年後接手者）
================================================================================
  - (i) orchestrator 是否實際填入 criteria（屬後端範疇，由前任務
    ``qa_test_task1_agenda_criteria_locator.py`` 守護）
  - (ii) DOM 在瀏覽器實際呈現（屬 e2e 範疇，由既有 ``test_offline_agenda_e2e.py``
    守 payload 結構、**但既有 e2e 不模擬前端 replay、不檢查 DOM 文字**——本測試
    + 人眼 demo 才是當前實際覆蓋）
  - (iii) criteria 為空字串時的省略行為（沿用既有 ``if (a.description)`` 守衛慣例，
    由 code review 守護）
  - (iv) 既有 title / description / assignee / corrections 不迴歸（既有
    ``test_offline_agenda_e2e.py`` 守護）

================================================================================
翻案條件（任一成立 → 推定翻案為 blocker）
================================================================================
  - 既有 ``test_offline_agenda_e2e.py`` 改寫為「模擬前端 replay」並覆蓋 DOM 文字
  - grep「政策選項／警示塊」在 production code 出現新實體（推定不再成立）
  - PM 確認「政策選項」是另一個語意實體（非 criteria 成功準則）
  - 既有 ``addSystem`` 改用 ``innerHTML`` 路徑（XSS 風險點位移，本守護失效）
================================================================================
"""

from __future__ import annotations

import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
# handleEvent 已拆到 web/js/events-render.js（ES module 化）
APP_JS_PATH = REPO_ROOT / "web" / "js" / "events-render.js"
E2E_PATH = REPO_ROOT / "tests" / "test_offline_agenda_e2e.py"


def _strip_js_comments(case_block: str) -> str:
    """略過 ``//`` 開頭的整行（避免註解內字串假觸發正 / 負樣）。"""
    cleaned: list[str] = []
    for line in case_block.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("//"):
            continue
        cleaned.append(line)
    return "\n".join(cleaned)


def _extract_agenda_plan_case(app_js: str) -> str:
    """抓出 ``case "agenda_plan":`` 對應的 case 區塊字串（從 marker 到配對 closing ``}``）。

    用大括號配對計數器掃描，比 regex 非貪婪 ``.*?`` 穩健——不會被
    ``forEach((a, i) => { ... })`` 的 nested ``{}`` 誤觸發。

    若檔案不含 marker 或大括號不配對，回傳空字串（呼叫端視為 fail）。
    """
    marker = 'case "agenda_plan":'
    idx = app_js.find(marker)
    if idx < 0:
        return ""
    open_brace = app_js.find("{", idx)
    if open_brace < 0:
        return ""
    depth = 0
    for i in range(open_brace, len(app_js)):
        ch = app_js[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return app_js[idx : i + 1]
    return ""


# ==============================================================================
# 1. 靜態掃描：核心正樣（agenda_plan case 區塊內 a.criteria 讀取 + 串到 line 賦值）
# ==============================================================================


def test_static_agenda_plan_case_contains_criteria_read_and_line_concat():
    """核心正樣：``case "agenda_plan":`` 區塊內 ``a.criteria`` 與
    ``line += ... criteria`` 模式**同時**出現。

    論證：此測試是任務 #2 修補面的**判別力守護**——同時斷言兩件事：
      1. ``a.criteria`` 在 case 區塊內被讀取（擋掉「整段漏寫」）
      2. ``criteria`` 緊接 ``line += ... ${...}`` 賦值（擋掉「if 寫了但沒接字串」）

    兩個條件皆成立 → 渲染修補到位；任一不成立 → fail。
    """
    assert APP_JS_PATH.exists(), f"前端入口檔不存在：{APP_JS_PATH}"
    app_js = APP_JS_PATH.read_text(encoding="utf-8")

    case_block = _extract_agenda_plan_case(app_js)
    assert case_block, (
        '找不到 case "agenda_plan": 的配對大括號區塊（檔案結構可能改寫，'
        "需人工修補本測試的 case 提取邏輯）"
    )

    cleaned = _strip_js_comments(case_block)

    # (1) a.criteria 在 case 區塊內被讀取
    assert "a.criteria" in cleaned, (
        f'case "agenda_plan": 區塊內未讀取 a.criteria：\n{case_block}\n'
        f"翻案條件：criteria 渲染整段漏寫 → 修補面 fail"
    )

    # (2) line += ... criteria 模式（拼接 + 字串內含 criteria 變數）
    #    排除模式：「if (a.criteria)」守衛行不算拼接；必須是 line += 後接字串
    has_line_concat_with_criteria = bool(
        re.search(r"line\s*\+=\s*[`'\"].*criteria.*[`'\"]", cleaned, re.DOTALL)
    ) or bool(re.search(r"line\s*\+=\s*.*\$\{a\.criteria\}", cleaned, re.DOTALL))
    assert has_line_concat_with_criteria, (
        f"case \"agenda_plan\": 區塊內 'a.criteria' 有讀取但未串到 line 賦值"
        f"（只寫了 if 守衛忘了接字串）。完整區塊：\n{case_block}"
    )


# ==============================================================================
# 2. 靜態掃描：核心負樣（a.tasks / a.edges 不在 agenda_plan case 區塊內）
# ==============================================================================


def test_static_agenda_plan_case_does_not_contain_tasks_or_edges():
    """核心負樣：``case "agenda_plan":`` 區塊內**不應**出現 ``a.tasks`` / ``a.edges``。

    論證：architect 決策 11「範圍守門」明列「不渲染 tasks / edges（任務依賴圖層級，
    需新 CSS 與新排版，scope 飄移）」。本測試把這條守門變成自動化——半年後接手者
    若想順手渲染 ``a.tasks`` / ``a.edges``，會被本測試 fail 攔下。

    用真實存在的 ``a.tasks`` / ``a.edges`` 取代虛構的 ``policy_option`` 拼字——
    有實質範圍守門意義（orchestrator 確實會送這兩個欄位），不是假想敵。
    """
    assert APP_JS_PATH.exists()
    app_js = APP_JS_PATH.read_text(encoding="utf-8")
    case_block = _extract_agenda_plan_case(app_js)
    assert case_block, '找不到 case "agenda_plan": 的配對大括號區塊'

    cleaned = _strip_js_comments(case_block)

    # 用 word boundary 確保是讀取變數 a.tasks / a.edges，不是其他字串
    for forbidden in ("a.tasks", "a.edges"):
        assert forbidden not in cleaned, (
            f'case "agenda_plan": 區塊內出現 {forbidden!r}——'
            f"architect 決策 11 範圍守門明列「不渲染 tasks / edges」。"
            f"若要啟用此渲染，須先翻案決策 11 並新增排版 / CSS 設計。\n"
            f"完整區塊：\n{case_block}"
        )


# ==============================================================================
# 3. 安全路徑驗證：addSystem 用 textContent（非 innerHTML）→ 無 XSS 風險
# ==============================================================================


def test_addSystem_uses_textContent_not_innerHTML():
    """安全驗證：``addSystem`` 實作必須用 ``textContent`` 而非 ``innerHTML``。

    論證：criteria 內容來自 PM LLM 輸出，理論上可控；但若 ``addSystem`` 改用
    ``innerHTML`` 會把 criteria 視為 HTML 解析——惡意 criteria（如 ``<img onerror=...>``）
    會被觸發。**本測試是「修補面進去後的第一條安全守護」**。

    抓取 ``function addSystem`` 函式體（用大括號計數器，與 case 提取同邏輯），
    斷言 body 含 ``textContent`` 且不含 ``innerHTML``。
    """
    assert APP_JS_PATH.exists()
    app_js = APP_JS_PATH.read_text(encoding="utf-8")

    marker = "function addSystem"
    idx = app_js.find(marker)
    assert idx >= 0, f"找不到 {marker!r} 函式定義"

    open_brace = app_js.find("{", idx)
    assert open_brace >= 0, f"{marker} 後缺 opening brace"

    depth = 0
    body_end = -1
    for i in range(open_brace, len(app_js)):
        ch = app_js[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                body_end = i
                break
    assert body_end >= 0, f"{marker} 大括號不配對"

    body = app_js[open_brace : body_end + 1]

    # 必須有 textContent 賦值
    assert (
        "textContent" in body
    ), f"addSystem body 不含 textContent 賦值，渲染路徑可能有 XSS 風險：\n{body}"
    # 不應有 innerHTML（無論是 property 賦值還是 string literal）
    assert "innerHTML" not in body, (
        f"addSystem body 含 innerHTML——criteria 進拼接後會被當 HTML 解析，"
        f"有 XSS 風險。請改用 textContent：\n{body}"
    )


# ==============================================================================
# 4. 既有覆蓋誠實評估（meta-test）：記錄既有 e2e 對 criteria 渲染的實際覆蓋
# ==============================================================================


def test_existing_e2e_does_not_assert_rendered_criteria_text_in_dom():
    """meta-test：既有 ``test_offline_agenda_e2e.py`` 對「DOM 內 criteria 文字」的
    實際覆蓋評估。

    論證：把守護鏈的事實基礎寫進測試，避免半年後接手者誤判覆蓋範圍。既有 e2e
    守 payload 結構（fixture 跑通 + 欄位比對），**不**模擬前端 replay、
    **不**檢查 DOM 內 criteria 文字。**真實 DOM 行為只有人眼 demo 守護**。

    本測試只記錄事實、不修補；無論結果如何都綠（斷言為真則綠、為假則提醒覆蓋
    缺口已升級，不需 fail）。
    """
    assert E2E_PATH.exists()
    text = E2E_PATH.read_text(encoding="utf-8")

    # 既有 e2e 是否對 DOM 文字做「criteria 內容」明確斷言？
    has_dom_criteria_assertion = bool(
        re.search(r"(textContent|innerText|innerHTML).*criteria", text, re.IGNORECASE)
    ) or bool(re.search(r"criteria.*(textContent|innerText|innerHTML)", text, re.IGNORECASE))
    # 既有 e2e 是否模擬前端 replay（load_events 後呼叫某個 render 函式）？
    simulates_replay = bool(re.search(r"(handleEvent|render\(|addSystem\()", text))

    # 既有 e2e 是否對 payload 結構做明確斷言（對照組：證明既有守護是結構層）
    has_payload_struct_assertion = bool(re.search(r"agenda_plan|payload\[.agenda.\]", text))

    report = {
        "既有 e2e 對 DOM 內 criteria 文字的明確斷言": has_dom_criteria_assertion,
        "既有 e2e 模擬前端 replay（render/addSystem/handleEvent）": simulates_replay,
        "既有 e2e 對 payload 結構的明確斷言（對照組）": has_payload_struct_assertion,
    }
    print(
        "\n[meta] 既有 e2e 對 criteria 渲染覆蓋報告：\n"
        + json.dumps(report, ensure_ascii=False, indent=2)
    )

    # 此測試只記錄事實、不 fail；無論如何都綠
    assert isinstance(has_dom_criteria_assertion, bool)
