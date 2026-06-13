"""QA 驗證：任務 #2 (round, speaker) 錨點程式化護欄（驗收 #2）。

聚焦 `_guard_anchor` / `_guard_list` 純函式判別力，以及經 summarize→record 後在
CONCLUSION.md 上「LLM 自填」與「有 transcript 來源」可視區分。

黑白樣本（含設計決策指定的判別力邊界黑樣本）：
  白：有效 (R<n> speaker) 且 speaker 真在 transcript → 不誤標。
  白：回填條目（走 _anchored_from_summary）→ 不過護欄、不被誤標。
  黑：無錨點 / speaker 不存在 transcript → 標 （未錨定）。
  黑（已知限制）：真 speaker＋幻覺論點 → **仍通過**（護欄只驗 speaker 出現，
       不驗論點對應），佐證設計決策標明的判別力上限。
"""

import asyncio

from studio import conclusion
from studio.discussion import Mention, Utterance


def _speakers():
    return {"engineer", "qa", "security"}


# ── _guard_anchor 純函式：白樣本（不該被標） ────────────────────────────────


def test_有效錨點_真speaker_不標():
    e = "engineer 與 senior 對齊混合範式 (R1 engineer)"
    assert conclusion._guard_anchor(e, _speakers()) == e


def test_多錨點_任一speaker屬實即不標():
    # 保守策略：含多錨點時只要任一 speaker 屬實即視為已錨定（寧漏標不誤傷）。
    e = "綜合 (R1 ghost) 與 (R2 engineer) 的意見"
    assert conclusion._guard_anchor(e, _speakers()) == e


def test_speaker含空白_精確比對不誤標():
    e = "結論 (R2 senior dev)"
    assert conclusion._guard_anchor(e, {"senior dev"}) == e


# ── _guard_anchor 純函式：黑樣本（該被標 （未錨定）） ────────────────────────


def test_無錨點_標未錨定():
    e = "上線時程未定"
    assert conclusion._guard_anchor(e, _speakers()) == "上線時程未定（未錨定）"


def test_speaker不存在transcript_標未錨定():
    # 有 (R<n> ...) 形狀但 speaker 是 transcript 沒出現過的幻覺名字。
    e = "ghost 主張全面重寫 (R3 ghost)"
    assert conclusion._guard_anchor(e, _speakers()) == "ghost 主張全面重寫 (R3 ghost)（未錨定）"


def test_錨點格式不符_標未錨定():
    # 缺 R 數字前綴、不符 (R<n> speaker) → 抽不到錨點。
    e = "某結論 (engineer)"
    assert conclusion._guard_anchor(e, _speakers()) == "某結論 (engineer)（未錨定）"


# ── 判別力邊界（已知限制，誠實暴露） ──────────────────────────────────────


def test_已知限制_真speaker加幻覺論點_仍通過():
    # 設計決策明載：護欄只驗 speaker 出現、不驗論點是否真對應 transcript pair。
    # LLM 借真名 engineer 編造從未發生的論點，因錨點 speaker 屬實 → 不被標記。
    # 這是護欄的判別力上限，非幻覺攔截保證——本測試固化此事實，防誤解。
    e = "engineer 主張全公司放假一年 (R1 engineer)"
    assert conclusion._guard_anchor(e, _speakers()) == e


# ── 冪等 ──────────────────────────────────────────────────────────────────


def test_已標未錨定_不重複標():
    e = "某結論（未錨定）"
    assert conclusion._guard_anchor(e, _speakers()) == e


def test_guard_list_逐條套用():
    entries = ["有來源 (R1 qa)", "沒來源"]
    out = conclusion._guard_list(entries, [Utterance(round=1, speaker="qa", text="x", mentions=[])])
    assert out == ["有來源 (R1 qa)", "沒來源（未錨定）"]


# ── 端到端：summarize→record 後 CONCLUSION.md 可視區分（驗收 #2） ───────────


def _summary():
    return {
        "consensus": ["engineer 同意 senior"],
        "disagreements": ["qa 反對 engineer"],
        "open_questions": ["qa 反對 engineer"],
        "unique_findings": ["security"],
        "final_positions": {"engineer": "採用混合範式", "qa": "覆蓋率不足"},
    }


def _transcript():
    return [
        Utterance(
            round=1,
            speaker="engineer",
            text="採用混合範式",
            mentions=[Mention(speaker="engineer", target="senior", stance="同意")],
        ),
        Utterance(round=2, speaker="qa", text="覆蓋率不足", mentions=[]),
    ]


class StubSenior:
    def __init__(self, output):
        self.output = output

    async def speak(self, prompt, broadcast):
        return self.output


async def _noop(ev):
    pass


def test_e2e_md可視區分_有來源不標_自產標未錨定(tmp_path):
    senior = StubSenior(
        "共識: engineer 與 senior 對齊 (R1 engineer)\n"  # 有效錨點 → 不標
        "分歧: qa 反對 engineer 的覆蓋率假設\n"  # 無錨點 → 標
        "未決: 時程未定\n"  # 無錨點 → 標
        "行動: 補測試"  # 無錨點 → 標
    )
    r = asyncio.run(conclusion.summarize(senior, _summary(), _transcript(), _noop))
    path = conclusion.record(tmp_path, r, session_id="s1", rounds=2)
    md = path.read_text(encoding="utf-8")
    # 有 transcript 來源的條目：保留錨點、不帶未錨定。
    assert "engineer 與 senior 對齊 (R1 engineer)" in md
    assert "engineer 與 senior 對齊 (R1 engineer)（未錨定）" not in md
    # LLM 自產無錨點條目：帶 （未錨定）。
    assert "qa 反對 engineer 的覆蓋率假設（未錨定）" in md
    assert "時程未定（未錨定）" in md
    assert "補測試（未錨定）" in md


def test_e2e_回填條目不被護欄誤標(tmp_path):
    # senior 只給行動，共識/分歧/未決走規則骨架回填 → 不過護欄、不該帶未錨定。
    senior = StubSenior("行動: 補 rate limit 測試")
    r = asyncio.run(conclusion.summarize(senior, _summary(), _transcript(), _noop))
    assert "（未錨定）" not in r["consensus"][0]
    assert "（未錨定）" not in r["disagreements"][0]
    assert "（未錨定）" not in r["open_questions"][0]
    # LLM 自產 actions 仍過護欄。
    assert r["actions"] == ["補 rate limit 測試（未錨定）"]
