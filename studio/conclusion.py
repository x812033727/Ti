"""結論彙整層 —— 把一場多角色討論蒸餾成結構化結論（共識／分歧／未決／行動）。

範式沿用 ADR 蒸餾的「規則式為骨、LLM 為肉」混合（見 NOTES 研究員調研）：

  1. 規則層 ``discussion._build_summary`` 已從 ``Mention.stance`` 統計出
     consensus / disagreements / open_questions / unique_findings（事實錨點、零 LLM、防幻覺）。
  2. 本模組以該骨架組 prompt，呼叫 senior 一次蒸餾出四段行前綴
     ``共識:／分歧:／未決:／行動:``，再用 :func:`studio.flow.parse_conclusion` 解析。
  3. senior 漏標前綴（解析回空骨架）時 **fallback** 回規則式 summary 骨架，不崩潰、
     仍產出可落盤的結論 dict（驗收 #6）。

防坑三條硬指令（字面寫入 prompt、可 grep 驗證）：
  ① 只彙整 transcript 出現過的論點，不得新增未提及的結論（防 Contextual Inference 幻覺）；
  ② 無人反對 ≠ 共識，需區分「明確同意」與「無人表態」（防 Silent Agreement 偏誤）；
  ③ 強分歧須保留並標明雙方，不得抹平。

落盤：:func:`record` 把彙整 dict 渲染成 ``CONCLUSION.md`` 四段 markdown（``## 共識／
## 分歧／## 未決事項／## 後續行動``），覆寫式單檔落 workspace 根（沿用 ``adr.py`` 的
cwd 定位與 atomic tmp-replace 慣例）。每場一份快照，歷史保存靠 git commit 而非 append
累積——commit 由 orchestrator 接線時以既有 ``self._commit`` 慣例執行（任務 #5），本模組
只負責「render＋落盤」，不直接呼叫 git，方便純檔案 IO 單元測試。

純邏輯與 LLM 呼叫解耦：``summarize`` 只依賴傳入的 ``senior.speak``，方便以 StubExpert 測試。
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from . import flow

if TYPE_CHECKING:  # 避免執行期循環匯入；型別僅供靜態檢查
    from .discussion import ExpertLike, Utterance


# 結論 dict 的固定四鍵，與 flow.parse_conclusion 回傳一致。
_KEYS = ("consensus", "disagreements", "open_questions", "actions")

# fallback 行動段佔位——末輪發言不是行動項，硬塞語意偏差（設計決策）；
# 寧可標明蒸餾失靈、留空，也不以 final_positions 冒充 action。
_FALLBACK_ACTION_NOTE = "（蒸餾失靈，無行動項）"


def _is_empty(parsed: dict[str, list[str]]) -> bool:
    """四鍵皆空 list ⇒ senior 全漏標前綴，視為蒸餾失靈、走 fallback。"""
    return not any(parsed.get(k) for k in _KEYS)


def _render_skeleton(summary: dict) -> str:
    """把規則式 summary 渲染成 prompt 用的事實骨架（帶 speaker 錨點，供 senior 引用）。

    只列規則層已統計出的事實，不外加任何推論——這是 senior「只彙整出現過的論點」的素材界。
    """
    lines: list[str] = []

    def _section(header: str, items: list[str]) -> None:
        lines.append(header)
        if items:
            lines.extend(f"  - {it}" for it in items)
        else:
            lines.append("  - （無）")

    _section("● 明確同意（stance=同意，已扣除同時反對者）：", list(summary.get("consensus") or []))
    _section("● 分歧（stance=反對）：", list(summary.get("disagreements") or []))
    _section(
        "● 未決（per-pair 末輪 stance 仍反對、未收斂）：",
        list(summary.get("open_questions") or []),
    )
    _section(
        "● 無人回應的角色發言（unique findings，僅供區分『無人表態』≠共識）：",
        list(summary.get("unique_findings") or []),
    )

    final_positions = summary.get("final_positions") or {}
    lines.append("● 各角色末輪立場（錨點來源，speaker 天生帶在此）：")
    if final_positions:
        for speaker, text in final_positions.items():
            snippet = (text or "").strip().replace("\n", " ")
            if len(snippet) > 120:
                snippet = snippet[:120] + "…"
            lines.append(f"  - {speaker}：{snippet}")
    else:
        lines.append("  - （無）")

    return "\n".join(lines)


def build_prompt(summary: dict, transcript: list[Utterance]) -> str:
    """以規則式 summary 為骨架組 senior 蒸餾 prompt，含三條防坑硬指令。

    錨點事實來源為規則層 summary（不信任 LLM 自填），故 prompt 提供 final_positions/
    unique_findings 的 speaker 錨點供其引用。
    """
    rounds = max((u.round for u in transcript), default=0)
    skeleton = _render_skeleton(summary)
    return (
        "你是高級工程師，請把剛才這場多角色討論蒸餾成一份結構化結論。\n"
        f"討論共 {rounds} 輪。以下是規則式統計出的事實骨架（唯一可信的論點來源）：\n\n"
        f"{skeleton}\n\n"
        "請逐行輸出，每行以下列四個前綴之一開頭（同一前綴可多行）：\n"
        "  共識: <雙方明確同意的點>\n"
        "  分歧: <仍有反對、需標明雙方立場的點>\n"
        "  未決: <尚未收斂、待後續釐清的問題>\n"
        "  行動: <可執行的後續待辦>\n\n"
        "硬性規則（違反即視為錯誤輸出）：\n"
        "① 只彙整上方骨架/transcript 出現過的論點，不得新增未提及的結論。\n"
        "② 無人反對 ≠ 共識：只有『明確同意』才算共識，『無人表態』不得列為共識。\n"
        "③ 強分歧必須保留並標明雙方，不得抹平成單一說法。\n"
        "每條結論盡量帶 (round, speaker) 錨點，例如「(R2 engineer)」，錨點須取自上方骨架。\n"
    )


def _fallback_from_summary(summary: dict) -> dict[str, list[str]]:
    """蒸餾失靈時的降級結論：直接用規則式 summary 骨架填四鍵。

    consensus→共識、disagreements→分歧、open_questions→未決；行動段留空並標明
    蒸餾失靈（不以末輪發言冒充 action，設計決策）。仍回完整四鍵 dict，呼叫端可正常落盤。
    """
    return {
        "consensus": list(summary.get("consensus") or []),
        "disagreements": list(summary.get("disagreements") or []),
        "open_questions": list(summary.get("open_questions") or []),
        "actions": [_FALLBACK_ACTION_NOTE],
    }


async def summarize(
    senior: ExpertLike,
    summary: dict,
    transcript: list[Utterance],
    broadcast,
) -> dict[str, list[str]]:
    """以規則骨架組 prompt、呼叫 senior 一次蒸餾、解析成四鍵結論 dict。

    senior 漏標前綴（``parse_conclusion`` 回空骨架）時 fallback 回規則式 summary 骨架，
    保證回傳完整四鍵 dict、永不拋例外，呼叫端據此落盤 CONCLUSION.md。

    :param senior: 任何具 ``async speak(prompt, broadcast) -> str`` 的專家（含 StubExpert）。
    :param summary: ``discussion.DiscussionResult.summary``（規則式五鍵）。
    :param transcript: 該場 ``Utterance`` 清單（僅用於取輪數）。
    :returns: ``{"consensus", "disagreements", "open_questions", "actions"}`` 四鍵 list dict。
    """
    prompt = build_prompt(summary, transcript)
    distilled = await senior.speak(prompt, broadcast)
    parsed = flow.parse_conclusion(distilled or "")
    if _is_empty(parsed):
        return _fallback_from_summary(summary)
    # 部分漏標：senior 標了某些前綴卻漏了別的（如只給 `行動:`）。此時規則層已知為真的
    # consensus/disagreements/open_questions 不可被靜默丟棄——空鍵以規則骨架回填，
    # 比整碗接受 LLM 殘缺輸出更穩（高工建議）。actions 規則層無對應來源，照 LLM 輸出。
    for key in ("consensus", "disagreements", "open_questions"):
        if not parsed.get(key):
            parsed[key] = list(summary.get(key) or [])
    return parsed


# ── 落盤（任務 #4）──────────────────────────────────────────────────────────
# CONCLUSION.md 固定四段，鍵→標題的對應（順序即渲染順序）。
_SECTIONS = (
    ("consensus", "共識"),
    ("disagreements", "分歧"),
    ("open_questions", "未決事項"),
    ("actions", "後續行動"),
)


def _md_path(cwd: Path) -> Path:
    return Path(cwd) / "CONCLUSION.md"


def render_markdown(conclusion: dict[str, list[str]]) -> str:
    """把四鍵結論 dict 渲染成固定四段 markdown，永遠輸出四段（空段標「（無）」）。

    每條結論逐條列為 bullet，原樣保留字串內的 ``(round, speaker)`` 錨點（錨點由規則層
    summary 帶入，見 ``summarize``）——不在此處增刪內容，純格式化。
    """
    lines = ["# 討論結論", ""]
    for key, title in _SECTIONS:
        lines.append(f"## {title}")
        items = [it.strip() for it in (conclusion.get(key) or []) if (it or "").strip()]
        if items:
            lines.extend(f"- {it}" for it in items)
        else:
            lines.append("- （無）")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def record(
    cwd: Path | None,
    conclusion: dict[str, list[str]],
    *,
    session_id: str = "",
) -> Path | None:
    """把結論 dict 渲染成 ``CONCLUSION.md`` 落 workspace 根（覆寫式單檔），回傳路徑。

    沿用 ``adr.py`` 的 atomic tmp-replace 寫入（避免半截檔）。每場覆寫——結論是本場快照，
    歷史保存靠 git commit（orchestrator #5 接線），非 append 累積。

    ``cwd`` 為 None（無 workspace 的單元測試）時不落盤、回 None；其餘永遠產出檔案
    （即便四鍵皆空也寫出四段骨架，確保 fallback 路徑仍有 CONCLUSION.md，驗收 #6）。

    :param conclusion: :func:`summarize` 回傳的四鍵 dict（已含 fallback 處理）。
    :param session_id: 僅供呼叫端紀錄/事件用，落盤本身不依賴。
    :returns: 寫出的 ``CONCLUSION.md`` 路徑，或 ``cwd is None`` 時 ``None``。
    """
    if cwd is None:
        return None
    path = _md_path(cwd)
    text = render_markdown(conclusion or {})
    tmp = path.with_suffix(".md.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
    return path
