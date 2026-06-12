"""通用多角色討論引擎（DiscussionEngine）。

支援任意 N 個角色（duck-typed 的 ExpertLike：`async speak(prompt, broadcast) -> str`）
跑多輪討論，兩種發言調度模式：

- ``round_robin``：同一輪內依 participants 順序逐一發言，後者可見同輪前者的發言。
- ``parallel``：同輪並行、輪間同步——每輪凍結「上一輪 transcript 快照」，全員基於同一份
  快照經 `asyncio.gather` 並行發言（每次 speak 包在注入的 semaphore 下節流），全部收齊
  才寫回 transcript（＝輪間同步屏障），無輪內競態。

context 餵法為「議題＋上一輪全員發言＋自己的歷史發言」（各段有截斷上限），刻意不重播
全史，避免 O(N²) token 膨脹。

本模組只依賴 stdlib 與 :mod:`studio.flow`、:mod:`studio.config`（皆無反向依賴），
**嚴禁 import orchestrator**（防循環依賴）；
semaphore / broadcast / should_stop 一律由呼叫端建構時注入。

實際介面簽名（驗收者以 inspect.signature 抽查，務必與程式碼一致）：

- ``DiscussionEngine.__init__(self, participants: list[tuple[str, ExpertLike]],
  mode: str = "round_robin", max_rounds: int | None = None,
  semaphore: AbstractAsyncContextManager | None = None,
  broadcast: Broadcast | None = None,
  should_stop: Callable[[], bool] | None = None,
  stall_threshold: float = 0.9)``

  ``max_rounds=None`` 時於建構當下取 :data:`studio.config.DISCUSS_MAX_ROUNDS`
  （env ``TI_DISCUSS_MAX_ROUNDS``，未設則退回 ``DEBATE_ROUNDS``）。
- ``async DiscussionEngine.run(self, topic: str) -> DiscussionResult``
- ``parse_mentions(speaker: str, text: str, participants: Sequence[str])
  -> list[Mention]`` — 解析發言中的 ``回應 @角色名: 同意|反對`` 結構化引用。
  防禦式：regex 以 participants 名單組白名單交替（名稱經 ``re.escape``），
  target 不在名單、格式不符或自我引用的片段一律丟棄；整段無合法匹配回傳
  空清單，絕不產生錯位結果。

反諂媚機制：engine 的發言 prompt 模板內建硬指令——回應其他角色必須用
``回應 @角色名: 同意|反對 ＋理由`` 結構化引用，且每輪至少指出一個可挑戰點，
無異議時必須說明為何（不可單純附和）。

資料結構：

- ``Mention(speaker: str, target: str, stance: str)`` — 結構化 @引用（由
  parse_mentions 解析發言全文後填入 Utterance.mentions）。
- ``Utterance(round: int, speaker: str, text: str, mentions: list[Mention])``
- ``DiscussionResult(transcript: list[Utterance], stop_reason: str, summary: dict)``
  其中 ``stop_reason ∈ {"max_rounds", "stalled", "cancelled"}``；summary 含
  ``consensus`` / ``disagreements`` / ``final_positions`` 三鍵。
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from collections.abc import Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from typing import Any, Protocol

from . import config, flow

Broadcast = Callable[[Any], Awaitable[None]]

# 各 context 區段的截斷上限（字元）：上一輪單人發言／自己單筆歷史發言。
# 防長討論 prompt 無限膨脹；從頭截尾保留最新內容語意（發言重點通常在結尾的結構化標記）。
PREV_SEGMENT_MAX_CHARS = 2000
SELF_SEGMENT_MAX_CHARS = 1200

_MODES = ("round_robin", "parallel")
_STOP_REASONS = ("max_rounds", "stalled", "cancelled")


class ExpertLike(Protocol):
    """發言者介面（結構性，與 orchestrator.ExpertLike 相容；此處不 import 它防循環依賴）。"""

    async def speak(self, prompt: str, broadcast: Broadcast) -> str: ...


@dataclass(frozen=True)
class Mention:
    """一筆結構化 @引用：speaker 對 target 表態（同意/反對）。"""

    speaker: str
    target: str
    stance: str  # "同意" | "反對"


@dataclass
class Utterance:
    """一筆發言：第幾輪（1-based）、誰說的、全文、解析出的 @引用。"""

    round: int
    speaker: str
    text: str
    mentions: list[Mention] = field(default_factory=list)


@dataclass
class DiscussionResult:
    """討論結果：結構化 transcript＋停止原因＋小結。

    stop_reason ∈ {"max_rounds", "stalled", "cancelled"}。
    summary 固定含三鍵：consensus（共識清單）、disagreements（分歧清單）、
    final_positions（{角色名: 末輪發言}）。
    """

    transcript: list[Utterance]
    stop_reason: str
    summary: dict


def parse_mentions(speaker: str, text: str, participants: Sequence[str]) -> list[Mention]:
    """解析發言全文中的 ``回應 @角色名: 同意|反對`` 結構化引用。

    防禦式設計（格式不符整段視為無引用，絕不 silent 錯位）：

    - regex 以 participants 名單組「白名單交替」（每個名稱經 ``re.escape``），
      而非通用 ``@(\\S+)`` 後再過濾——target 不在名單的片段根本不會匹配。
    - 名稱交替依長度遞減排序，避免短名稱是長名稱前綴時搶先匹配造成錯位
      （如「甲」vs「甲乙」）。
    - 立場僅接受「同意」「反對」二值；缺冒號、立場詞不符等格式錯誤的片段不匹配、
      直接丟棄。
    - 自我引用（target == speaker）視為格式誤用丟棄。
    - 整段無任何合法匹配 → 回傳空清單。
    """
    if not text or not participants:
        return []
    names = sorted((n for n in participants if n), key=len, reverse=True)
    if not names:
        return []
    alternation = "|".join(re.escape(n) for n in names)
    pattern = re.compile(rf"回應\s*@({alternation})\s*[:：]\s*(同意|反對)")
    mentions: list[Mention] = []
    for m in pattern.finditer(text):
        target, stance = m.group(1), m.group(2)
        if target == speaker:
            continue
        mentions.append(Mention(speaker=speaker, target=target, stance=stance))
    return mentions


async def _noop_broadcast(_event: Any) -> None:
    return None


class DiscussionEngine:
    """N 角色討論循環。participants 為 (名稱, expert) 列表，順序即發言/寫回順序。

    - max_rounds：最大輪數硬上限；None＝建構時取 config.DISCUSS_MAX_ROUNDS
      （env TI_DISCUSS_MAX_ROUNDS，未設退回 DEBATE_ROUNDS）。
    - semaphore：注入的並發節流（如 orchestrator._llm_semaphore()）；None＝不節流。
    - broadcast：speak 轉手用的事件回呼；None＝no-op。
    - should_stop：每輪開頭檢查，True 即停（stop_reason="cancelled"）。
    - stall_threshold：連續輪次「全員發言串接」相似度 ≥ 此值即提前停（stop_reason="stalled"）。
    """

    def __init__(
        self,
        participants: list[tuple[str, ExpertLike]],
        mode: str = "round_robin",
        max_rounds: int | None = None,
        semaphore: AbstractAsyncContextManager | None = None,
        broadcast: Broadcast | None = None,
        should_stop: Callable[[], bool] | None = None,
        stall_threshold: float = 0.9,
    ):
        if mode not in _MODES:
            raise ValueError(f"mode 必須是 {_MODES} 之一，收到 {mode!r}")
        if max_rounds is None:
            # 建構當下讀即時全域值（config.reload() 後新建的 engine 即生效）。
            max_rounds = config.DISCUSS_MAX_ROUNDS
        if max_rounds < 1:
            raise ValueError(f"max_rounds 必須 ≥ 1，收到 {max_rounds}")
        names = [name for name, _ in participants]
        if not names:
            raise ValueError("participants 不可為空")
        for name in names:
            # 名稱會進 prompt 的 `回應 @名稱:` 模板與 parse_mentions 的 regex 白名單；
            # 空白字元會讓 `@名稱` 邊界歧義，直接拒收。re.escape 後必可安全入 regex。
            if not name or re.search(r"\s", name):
                raise ValueError(f"角色名稱不可為空或含空白字元：{name!r}")
        if len(set(names)) != len(names):
            raise ValueError(f"角色名稱必須唯一：{names}")

        self._participants = list(participants)
        self._names = names  # parse_mentions 的白名單（順序同 participants）
        self._mode = mode
        self._max_rounds = max_rounds
        self._semaphore = semaphore
        self._broadcast: Broadcast = broadcast or _noop_broadcast
        self._should_stop = should_stop or (lambda: False)
        self._stall_threshold = stall_threshold

    # --- context 組裝 ---------------------------------------------------
    @staticmethod
    def _clip(text: str, cap: int) -> str:
        text = (text or "").strip()
        if len(text) <= cap:
            return text
        return "…（前段截斷）" + text[-cap:]

    def _build_prompt(
        self,
        name: str,
        topic: str,
        round_no: int,
        prev_round: list[Utterance],
        own_history: list[Utterance],
    ) -> str:
        """組某角色本輪的發言 prompt：議題＋上一輪全員發言＋自己歷史發言（不重播全史）。"""
        others = "、".join(f"@{n}" for n, _ in self._participants if n != name)
        parts = [
            f"【多角色討論】議題：\n{topic}\n",
            f"你是 {name}，正在與 {others} 進行第 {round_no} 輪討論。",
        ]
        if prev_round:
            lines = [
                f"@{u.speaker}：{self._clip(u.text, PREV_SEGMENT_MAX_CHARS)}" for u in prev_round
            ]
            parts.append("【上一輪全員發言】\n" + "\n\n".join(lines))
        if own_history:
            lines = [
                f"第 {u.round} 輪：{self._clip(u.text, SELF_SEGMENT_MAX_CHARS)}"
                for u in own_history
            ]
            parts.append("【你先前的發言】\n" + "\n\n".join(lines))
        parts.append(
            "請針對議題發表本輪意見，精簡聚焦。"
            if round_no == 1 and not prev_round
            else "請針對其他角色的發言與議題發表本輪意見，精簡聚焦。"
        )
        # 反諂媚硬指令＋結構化引用格式（任務 #2）：固定附在每輪 prompt 末尾。
        parts.append(
            "【發言格式硬性要求】\n"
            "1. 回應其他角色時，必須使用結構化引用，每條獨立一行、格式嚴格如下：\n"
            "   回應 @角色名: 同意 ＋理由\n"
            "   回應 @角色名: 反對 ＋理由\n"
            f"   角色名僅限：{others}；立場僅限「同意」或「反對」二選一，後面必須附具體理由。\n"
            "2. 反諂媚：你必須至少指出一個可挑戰點（其他角色論點的弱點、風險、盲區，"
            "或議題本身的疑慮）；若你對所有發言皆無異議，必須明確說明為何無異議，"
            "不可單純附和或重複他人觀點。"
        )
        return "\n\n".join(parts)

    # --- 發言（經注入 semaphore 節流）------------------------------------
    async def _speak(self, expert: ExpertLike, prompt: str) -> str:
        sem = self._semaphore if self._semaphore is not None else contextlib.nullcontext()
        async with sem:
            return await expert.speak(prompt, self._broadcast)

    # --- 主循環 -----------------------------------------------------------
    async def run(self, topic: str) -> DiscussionResult:
        """跑完整討論循環，回傳結構化 transcript／停止原因／小結。"""
        transcript: list[Utterance] = []
        own: dict[str, list[Utterance]] = {name: [] for name, _ in self._participants}
        prev_round: list[Utterance] = []
        round_history: list[str] = []  # 每輪「全員發言依 participants 順序串接」，餵 is_stalled
        stop_reason = "max_rounds"

        for round_no in range(1, self._max_rounds + 1):
            if self._should_stop():
                stop_reason = "cancelled"
                break

            if self._mode == "parallel":
                # 同輪並行：全員基於同一份 prev_round 快照發言（prev_round 在本輪內不變），
                # gather 全收齊才寫回 transcript ＝ 輪間同步屏障，無輪內競態。
                prompts = [
                    self._build_prompt(name, topic, round_no, prev_round, own[name])
                    for name, _ in self._participants
                ]
                texts = await asyncio.gather(
                    *(
                        self._speak(expert, prompt)
                        for (_, expert), prompt in zip(self._participants, prompts, strict=True)
                    )
                )
                # 寫回固定依 participants 順序（gather 保序）：transcript 順序與
                # round_history 串接順序皆確定，避免順序抖動讓 is_stalled 誤判相似度。
                this_round = [
                    Utterance(round_no, name, text, parse_mentions(name, text, self._names))
                    for (name, _), text in zip(self._participants, texts, strict=True)
                ]
            else:  # round_robin：同輪內依序發言，後者可見同輪前者（prev_round＋同輪累積）。
                this_round = []
                for name, expert in self._participants:
                    if self._should_stop():
                        break
                    prompt = self._build_prompt(
                        name, topic, round_no, prev_round + this_round, own[name]
                    )
                    text = await self._speak(expert, prompt)
                    this_round.append(
                        Utterance(round_no, name, text, parse_mentions(name, text, self._names))
                    )
                if len(this_round) < len(self._participants):
                    # 輪中被要求停止：已完成的發言保留進 transcript，標 cancelled。
                    transcript.extend(this_round)
                    stop_reason = "cancelled"
                    break

            transcript.extend(this_round)
            for u in this_round:
                own[u.speaker].append(u)
            prev_round = this_round

            # 停滯偵測：連續輪次「全員發言串接」高度相似即提前停（沿用 flow.is_stalled）。
            round_history.append("\n".join(u.text for u in this_round))
            if round_no < self._max_rounds and flow.is_stalled(
                round_history, rounds=2, threshold=self._stall_threshold
            ):
                stop_reason = "stalled"
                break

        return DiscussionResult(
            transcript=transcript,
            stop_reason=stop_reason,
            summary=self._build_summary(transcript),
        )

    # --- 小結（規則式、零 LLM 呼叫）---------------------------------------
    def _build_summary(self, transcript: list[Utterance]) -> dict:
        """從 transcript 推導小結（規則式、零 LLM 呼叫）。共識/分歧由各 Utterance 的
        mentions（parse_mentions 解析結果）統計推導；final_positions 取各角色末輪發言。"""
        final_positions: dict[str, str] = {}
        for u in transcript:
            final_positions[u.speaker] = u.text
        agree: set[tuple[str, str]] = set()
        disagree: set[tuple[str, str]] = set()
        for u in transcript:
            for m in u.mentions:
                pair = (m.speaker, m.target)
                if m.stance == "同意":
                    agree.add(pair)
                elif m.stance == "反對":
                    disagree.add(pair)
        consensus = [f"{s} 同意 {t}" for s, t in sorted(agree - disagree)]
        disagreements = [f"{s} 反對 {t}" for s, t in sorted(disagree)]
        return {
            "consensus": consensus,
            "disagreements": disagreements,
            "final_positions": final_positions,
        }
