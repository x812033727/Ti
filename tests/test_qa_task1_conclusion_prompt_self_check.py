"""QA 驗證：任務 #1 的 conclusion prompt 第④條自我校驗硬指令。

重點不是只 grep 到關鍵字，而是防止兩種假綠：
1. 第④條被插在中間或後面又追加其他指令，失去「末尾自檢」語意。
2. 為了自我校驗多打一次 senior/LLM，違反零新增 LLM 呼叫。
"""

import asyncio

from studio import conclusion
from studio.discussion import Utterance


class CountingSenior:
    def __init__(self, output: str):
        self.output = output
        self.calls = 0
        self.prompts: list[str] = []

    async def speak(self, prompt, broadcast):
        self.calls += 1
        self.prompts.append(prompt)
        return self.output


async def _noop(_event):
    return None


def _summary():
    return {
        "consensus": ["engineer 同意採用混合範式"],
        "disagreements": ["qa 反對覆蓋率假設"],
        "open_questions": ["release gate 尚未定案"],
        "unique_findings": ["pm 要求可重現驗收"],
        "final_positions": {
            "engineer": "採用混合範式",
            "qa": "覆蓋率不足",
        },
    }


def _transcript():
    return [
        Utterance(round=1, speaker="engineer", text="採用混合範式", mentions=[]),
        Utterance(round=2, speaker="qa", text="覆蓋率不足", mentions=[]),
    ]


def test_第四條自我校驗是最後一條硬規則且包含來源約束():
    prompt = conclusion.build_prompt(_summary(), _transcript())
    lines = [line.strip() for line in prompt.splitlines() if line.strip()]

    positions = {
        marker: next(i for i, line in enumerate(lines) if line.startswith(marker))
        for marker in ("①", "②", "③", "④")
    }

    assert positions["①"] < positions["②"] < positions["③"] < positions["④"]
    assert positions["④"] == len(lines) - 1, "第④條後不得再追加會稀釋自檢語意的 prompt 文字"

    rule4 = lines[positions["④"]]
    for required in (
        "逐條自我校驗",
        "每一條結論",
        "能對應上方骨架",
        "(round, speaker)",
        "須取自上方骨架",
        "查無骨架依據者一律刪除",
        "不得保留沒有來源的結論",
    ):
        assert required in rule4


def test_第四條自我校驗不增加_senior_llm_呼叫次數():
    senior = CountingSenior(
        "共識: engineer 同意採用混合範式 (R1 engineer)\n"
        "分歧: qa 反對覆蓋率假設 (R2 qa)\n"
        "未決: release gate 尚未定案 (R2 qa)\n"
        "行動: 補齊驗收測試 (R1 engineer)"
    )

    result = asyncio.run(conclusion.summarize(senior, _summary(), _transcript(), _noop))

    assert senior.calls == 1
    assert len(senior.prompts) == 1
    assert "逐條自我校驗" in senior.prompts[0]
    assert result["actions"] == ["補齊驗收測試 (R1 engineer)"]
