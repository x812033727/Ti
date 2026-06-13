"""QA 驗證：conclusion.summarize 彙整層（任務 #3 驗收 #1/#6）。

涵蓋：① senior 正常輸出四前綴 → 正確解析成四鍵；② prompt 以規則骨架組裝且含三條
防坑硬指令與 speaker 錨點來源；③ senior 全漏標前綴/空輸出 → fallback 退回規則式
summary 骨架、行動段標明蒸餾失靈不冒充（驗收 #6）。
"""

import asyncio

from studio import conclusion
from studio.discussion import Utterance


class StubSenior:
    """記錄收到的 prompt、回傳預設文字的假 senior（對齊 StubExpert 慣例）。"""

    def __init__(self, output: str):
        self.output = output
        self.prompt = None

    async def speak(self, prompt, broadcast):
        self.prompt = prompt
        return self.output


async def _noop(ev):
    pass


def _summary():
    return {
        "consensus": ["engineer 同意 senior"],
        "disagreements": ["qa 反對 engineer"],
        "open_questions": ["qa 反對 engineer"],
        "unique_findings": ["security"],
        "final_positions": {
            "engineer": "採用混合範式",
            "qa": "覆蓋率不足",
            "security": "要加 rate limit",
        },
    }


def _transcript():
    return [
        Utterance(round=1, speaker="engineer", text="採用混合範式", mentions=[]),
        Utterance(round=2, speaker="qa", text="覆蓋率不足", mentions=[]),
    ]


def test_正常解析四前綴():
    # 合規 senior：每條結論都帶取自骨架的有效錨點（speaker 存在於 transcript），
    # 護欄（#2）全數放行、不加 （未錨定），原文照用。
    senior = StubSenior(
        "共識: engineer 與 senior 對齊混合範式 (R1 engineer)\n"
        "分歧: qa 反對 engineer 的覆蓋率假設 (R2 qa)\n"
        "未決: 上線時程未定 (R2 qa)\n"
        "行動: 補測試覆蓋 (R1 engineer)"
    )
    r = asyncio.run(conclusion.summarize(senior, _summary(), _transcript(), _noop))
    # #2 護欄：有效 (R1 engineer) 錨點（engineer 在 transcript）→ 不誤標。
    assert r["consensus"] == ["engineer 與 senior 對齊混合範式 (R1 engineer)"]
    # 其餘條目皆帶取自骨架的有效錨點（speaker 在 transcript）→ 護欄放行、不誤標。
    assert r["disagreements"] == ["qa 反對 engineer 的覆蓋率假設 (R2 qa)"]
    assert r["open_questions"] == ["上線時程未定 (R2 qa)"]
    assert r["actions"] == ["補測試覆蓋 (R1 engineer)"]


def test_prompt_含四條防坑硬指令與錨點來源():
    senior = StubSenior("共識: x")
    asyncio.run(conclusion.summarize(senior, _summary(), _transcript(), _noop))
    p = senior.prompt
    assert "不得新增未提及的結論" in p  # ① 防幻覺
    assert "無人反對 ≠ 共識" in p  # ② 防 Silent Agreement
    assert "強分歧必須保留並標明雙方" in p  # ③ 保留分歧
    # ④ 自我校驗（任務 #1）：逐條自檢、查無依據者刪除，須可 grep（驗收 #1）
    assert "④" in p
    assert "逐條自我校驗" in p
    assert "查無骨架依據者一律刪除" in p
    # 四鍵前綴未被第④條擠散，仍可被 parse_conclusion 解析（驗收 #1）
    for prefix in ("共識:", "分歧:", "未決:", "行動:"):
        assert prefix in p
    # 錨點事實來源為規則骨架：speaker 帶在 final_positions / unique_findings
    assert "採用混合範式" in p
    assert "security" in p


def test_全漏標前綴_fallback_退回規則骨架():
    senior = StubSenior("這是一段沒有任何前綴的自由文字，senior 沒按格式輸出。")
    r = asyncio.run(conclusion.summarize(senior, _summary(), _transcript(), _noop))
    assert r["consensus"] == ["engineer 同意 senior"]
    assert r["disagreements"] == ["qa 反對 engineer"]
    assert r["open_questions"] == ["qa 反對 engineer"]
    # 行動段不以末輪發言冒充，標明蒸餾失靈（設計決策）
    assert r["actions"] == ["（蒸餾失靈，無行動項）"]


def test_空輸出也不崩潰走_fallback():
    r = asyncio.run(conclusion.summarize(StubSenior(""), _summary(), _transcript(), _noop))
    assert r["actions"] == ["（蒸餾失靈，無行動項）"]
    assert r["consensus"] == ["engineer 同意 senior"]


def test_部分漏標_空鍵以規則骨架回填():
    # senior 只給了行動，漏標共識/分歧/未決——規則層已知為真者不可被靜默丟棄
    senior = StubSenior("行動: 補 rate limit 測試")
    r = asyncio.run(conclusion.summarize(senior, _summary(), _transcript(), _noop))
    # LLM 自產的 action 無 (round, speaker) 錨點 → 護欄（#2）標 （未錨定），與「有 transcript
    # 來源」可視區分
    assert r["actions"] == ["補 rate limit 測試（未錨定）"]
    # 空鍵回填規則骨架——走 _anchored_from_summary、帶 transcript 真錨點，不被護欄重複標記
    assert r["consensus"] == ["engineer 同意 senior"]
    assert r["disagreements"] == ["qa 反對 engineer"]
    assert r["open_questions"] == ["qa 反對 engineer"]


def test_回傳固定四鍵齊全():
    senior = StubSenior("共識: 只有共識一行")
    r = asyncio.run(conclusion.summarize(senior, _summary(), _transcript(), _noop))
    assert set(r) == {"consensus", "disagreements", "open_questions", "actions"}


# ── 任務 #1：第④條自我校驗硬指令 ──────────────────────────────────────────────


def test_prompt_含第四條自我校驗指令():
    """build_prompt 末尾須可 grep 到第④條自我校驗指令，且不破壞四前綴格式（驗收 #1）。"""
    p = conclusion.build_prompt(_summary(), _transcript())
    assert "④" in p
    assert "逐條自我校驗" in p
    assert "查無骨架依據者一律刪除" in p
    # 四前綴硬指令骨架不被破壞：四個輸出前綴仍在。
    for prefix in ("共識:", "分歧:", "未決:", "行動:"):
        assert prefix in p


def test_第四條指令不被誤解析成結論():
    """第④條／錨點指令行不以四前綴開頭，不會被 parse_conclusion 誤抽成結論條目（驗收 #1）。

    （prompt 的『格式說明行』如 `共識: <雙方...>` 本就會被解析，那是既有行為、與 ④ 無關；
    這裡只驗 ④ 與錨點指令的文字不混入任何結論值。）
    """
    from studio import flow

    p = conclusion.build_prompt(_summary(), _transcript())
    parsed = flow.parse_conclusion(p)
    flat = [item for items in parsed.values() for item in items]
    assert not any("逐條自我校驗" in it for it in flat)
    assert not any("查無骨架依據者一律刪除" in it for it in flat)
    # senior 真實輸出仍能被正確解析（端到端不破壞）已由 test_正常解析四前綴 覆蓋。


# ── 任務 #2：錨點程式化護欄 _guard_anchor ─────────────────────────────────────


def test_guard_有效錨點且speaker存在_放行():
    speakers = {"engineer", "qa"}
    e = "雙方對齊混合範式 (R1 engineer)"
    assert conclusion._guard_anchor(e, speakers) == e  # 不標未錨定


def test_guard_無錨點_標未錨定():
    speakers = {"engineer", "qa"}
    assert (
        conclusion._guard_anchor("一條沒有錨點的結論", speakers) == "一條沒有錨點的結論（未錨定）"
    )


def test_guard_錨點speaker不存在_標未錨定():
    """黑樣本：錨點 token 格式對，但 speaker 不在 transcript → 標未錨定。"""
    speakers = {"engineer", "qa"}
    assert (
        conclusion._guard_anchor("幻覺結論 (R1 ghost)", speakers) == "幻覺結論 (R1 ghost)（未錨定）"
    )


def test_guard_真speaker加幻覺論點_漏標_已知限制():
    """已知限制黑樣本：speaker 真實存在、論點卻是幻覺，護欄只驗 speaker 出現 → 放行漏標。

    這證明 _guard_anchor 是『盡力而為』而非幻覺攔截保證（設計決策誠實標明的判別力上限）。
    """
    speakers = {"engineer", "qa"}
    e = "engineer 主張用區塊鏈重寫整個系統 (R1 engineer)"  # engineer 存在，但這論點是幻覺
    assert conclusion._guard_anchor(e, speakers) == e  # 漏標——已知限制，非 bug


def test_guard_已標未錨定_不重複標():
    speakers = {"engineer"}
    e = "某結論（未錨定）"
    assert conclusion._guard_anchor(e, speakers) == e
