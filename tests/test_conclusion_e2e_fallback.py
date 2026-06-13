"""離線 e2e 回歸：兩條既有 fallback 路徑跑完整鏈路（驗收 #5）。

涵蓋 build_summary → summarize（senior 觸發 fallback）→ record 落盤 整條離線鏈路，
不需真實 LLM。驗證 #1~#4 三項強化（第④條自我校驗、未錨定護欄、JSON sidecar）皆無回歸：

- 路徑 A「全員無反對」：transcript 全為「同意」mention，disagreements 空、consensus 有料。
- 路徑 B「LLM 漏標前綴」：senior 回無四前綴的雜訊 → parse_conclusion 回空 → fallback。

兩條皆斷言：(1) CONCLUSION.md 四段標題齊全；(2) 至少一條結論回指 transcript（帶
``(R<n> <speaker>)`` 錨點，且該 speaker 真在 transcript 出現——自證對應、排除假綠）；
(3) sidecar conclusion.json 同步落盤、四鍵＋session_id＋rounds 合法。
"""

import asyncio
import json
import re

from studio import conclusion
from studio.discussion import Mention, Utterance, build_summary

_SECTION_TITLES = ("## 共識", "## 分歧", "## 未決事項", "## 後續行動")
# 回指 transcript 的錨點 token：(R<數字> <speaker>)。
_ANCHOR_RE = re.compile(r"\(R(\d+)\s+([^)]+)\)")


class StubSenior:
    """回傳預設文字的假 senior（對齊既有測試的 StubSenior 慣例）。"""

    def __init__(self, output: str):
        self.output = output

    async def speak(self, prompt, broadcast):
        return self.output


async def _noop(_ev):
    pass


def _assert_four_sections(md: str):
    for title in _SECTION_TITLES:
        assert title in md, f"缺四段標題：{title}"


def _assert_anchors_point_to_transcript(md: str, transcript):
    """至少一條結論帶錨點，且錨點 speaker 真在 transcript 出現（排除假綠）。"""
    speakers = {u.speaker for u in transcript}
    anchors = _ANCHOR_RE.findall(md)
    assert anchors, "fallback 結論至少要有一條帶 (R<n> <speaker>) 錨點回指 transcript"
    for _round, speaker in anchors:
        assert speaker.strip() in speakers, f"錨點 speaker {speaker!r} 不在 transcript，疑似假錨"


def _run_record(senior, transcript, tmp_path, session_id, rounds):
    summary = build_summary(transcript)
    result = asyncio.run(conclusion.summarize(senior, summary, transcript, _noop))
    path = conclusion.record(tmp_path, result, session_id=session_id, rounds=rounds)
    return path, result


def test_e2e_全員無反對_fallback_四段且回指transcript(tmp_path):
    # 全員「同意」：build_summary → consensus 有料、disagreements 空。
    transcript = [
        Utterance(round=1, speaker="engineer", text="採用混合範式", mentions=[]),
        Utterance(
            round=2,
            speaker="qa",
            text="回應 @engineer: 同意 覆蓋率可接受",
            mentions=[Mention(speaker="qa", target="engineer", stance="同意")],
        ),
    ]
    summary = build_summary(transcript)
    assert summary["consensus"], "全員無反對應產生 consensus"
    assert not summary["disagreements"], "全員無反對 disagreements 應為空"

    # senior 全漏標前綴（空輸出）→ summarize 走 fallback → record 落盤。
    path, result = _run_record(StubSenior(""), transcript, tmp_path, "sess-A", rounds=2)
    md = path.read_text(encoding="utf-8")

    _assert_four_sections(md)
    # fallback 的 consensus 由規則骨架帶錨點，回指 transcript（qa 同意 engineer → R2 qa）。
    _assert_anchors_point_to_transcript(md, transcript)
    assert "qa 同意 engineer" in md

    # sidecar 同步落盤、合法 JSON、四鍵＋session_id＋rounds。
    sidecar = json.loads((tmp_path / "conclusion.json").read_text(encoding="utf-8"))
    assert sidecar["session_id"] == "sess-A"
    assert sidecar["rounds"] == 2
    for key in ("consensus", "disagreements", "open_questions", "actions"):
        assert key in sidecar


def test_e2e_LLM漏標前綴_fallback_四段且回指transcript(tmp_path):
    # transcript 含一筆反對 → disagreements 有料，確保 fallback 的分歧段也回指 transcript。
    transcript = [
        Utterance(round=1, speaker="engineer", text="主張全量重算", mentions=[]),
        Utterance(
            round=2,
            speaker="security",
            text="回應 @engineer: 反對 成本過高",
            mentions=[Mention(speaker="security", target="engineer", stance="反對")],
        ),
    ]
    summary = build_summary(transcript)
    assert summary["disagreements"], "應有分歧"

    # senior 回一段毫無四前綴的雜訊 → parse_conclusion 回空骨架 → _is_empty → fallback。
    noisy = "這是一段沒有任何結論前綴的閒聊，模型忘了照格式輸出。"
    path, result = _run_record(StubSenior(noisy), transcript, tmp_path, "sess-B", rounds=2)
    md = path.read_text(encoding="utf-8")

    _assert_four_sections(md)
    _assert_anchors_point_to_transcript(md, transcript)
    assert "security 反對 engineer" in md
    # 雜訊未被當成結論塞進任何段落（排除「漏標卻照收 LLM 原文」回歸）。
    assert "閒聊" not in md

    sidecar = json.loads((tmp_path / "conclusion.json").read_text(encoding="utf-8"))
    assert sidecar["session_id"] == "sess-B"
    assert sidecar["rounds"] == 2
    assert sidecar["disagreements"], "sidecar 分歧段應與 md 一致有料"
