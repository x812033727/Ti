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
    senior = StubSenior(
        "共識: engineer 與 senior 對齊混合範式 (R1 engineer)\n"
        "分歧: qa 反對 engineer 的覆蓋率假設\n"
        "未決: 上線時程未定\n"
        "行動: 補測試覆蓋"
    )
    r = asyncio.run(conclusion.summarize(senior, _summary(), _transcript(), _noop))
    # #2 護欄：有效 (R1 engineer) 錨點（engineer 在 transcript）→ 不誤標。
    assert r["consensus"] == ["engineer 與 senior 對齊混合範式 (R1 engineer)"]
    # 其餘 LLM 自產條目無錨點 → 標 （未錨定），可視區分「LLM 自填」與「有來源」。
    assert r["disagreements"] == ["qa 反對 engineer 的覆蓋率假設（未錨定）"]
    assert r["open_questions"] == ["上線時程未定（未錨定）"]
    assert r["actions"] == ["補測試覆蓋（未錨定）"]


def test_prompt_含三條防坑硬指令與錨點來源():
    senior = StubSenior("共識: x")
    asyncio.run(conclusion.summarize(senior, _summary(), _transcript(), _noop))
    p = senior.prompt
    assert "不得新增未提及的結論" in p  # ① 防幻覺
    assert "無人反對 ≠ 共識" in p  # ② 防 Silent Agreement
    assert "強分歧必須保留並標明雙方" in p  # ③ 保留分歧
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
    # LLM 給的 actions 照用，但無錨點 → 過護欄標 （未錨定）。
    assert r["actions"] == ["補 rate limit 測試（未錨定）"]
    # 空鍵回填規則骨架：走 _anchored_from_summary，不過護欄（不會被誤標）。
    assert r["consensus"] == ["engineer 同意 senior"]  # 空鍵回填規則骨架
    assert r["disagreements"] == ["qa 反對 engineer"]
    assert r["open_questions"] == ["qa 反對 engineer"]


def test_回傳固定四鍵齊全():
    senior = StubSenior("共識: 只有共識一行")
    r = asyncio.run(conclusion.summarize(senior, _summary(), _transcript(), _noop))
    assert set(r) == {"consensus", "disagreements", "open_questions", "actions"}
