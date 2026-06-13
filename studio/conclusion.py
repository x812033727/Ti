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

import contextlib
import json
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING

from . import flow

if TYPE_CHECKING:  # 避免執行期循環匯入；型別僅供靜態檢查
    from .discussion import ExpertLike, Utterance

log = logging.getLogger("ti.conclusion")


# 結論 dict 的固定四鍵，與 flow.parse_conclusion 回傳一致。
_KEYS = ("consensus", "disagreements", "open_questions", "actions")

# fallback 行動段佔位——末輪發言不是行動項，硬塞語意偏差（設計決策）；
# 寧可標明蒸餾失靈、留空，也不以 final_positions 冒充 action。
_FALLBACK_ACTION_NOTE = "（蒸餾失靈，無行動項）"


def _is_empty(parsed: dict[str, list[str]]) -> bool:
    """四鍵皆空 list ⇒ senior 全漏標前綴，視為蒸餾失靈、走 fallback。"""
    return not any(parsed.get(k) for k in _KEYS)


# ── (round, speaker) 錨點：事實來源＝規則層 summary ＋ transcript，不賭 LLM ──────
#
# 規則層 ``_build_summary`` 的 consensus/disagreements/open_questions 條目格式固定為
# ``f"{speaker} 同意/反對 {target}"``（無 round）；錨點所需的 round 在 transcript 的
# Utterance.round 裡。本層把兩者對齊：對每條規則條目，回查它所依據的 mention pair 在
# transcript 出現的末輪 round，附上 ``(R<round> <speaker>)`` 錨點。
#
# 為什麼放在 conclusion 層而非改 ``_build_summary``：#2 的三鍵格式為架構師凍結合約
# （tests/core/test_discussion.py 精確比對），改它會跨 lane 破壞回歸；而「每條盡量帶
# 錨點」本就是本任務（#4）職責，且 ``summarize`` 已收到 transcript，原料齊備。
# 透過「以已知 pair 反向重建字串精確比對」對齊，不靠 split 字串（角色名可能含空白）。

_VERB = {"consensus": "同意", "disagreements": "反對", "open_questions": "反對"}


def _pair_rounds(transcript: list[Utterance]) -> dict[tuple[str, str], int]:
    """transcript 中每個 (speaker, target) mention pair 的末輪 round（取最大）。"""
    rounds: dict[tuple[str, str], int] = {}
    for u in transcript:
        for m in u.mentions:
            pair = (m.speaker, m.target)
            if pair not in rounds or u.round > rounds[pair]:
                rounds[pair] = u.round
    return rounds


def _anchor_list(
    entries: list[str], verb: str, pair_rounds: dict[tuple[str, str], int]
) -> list[str]:
    """對規則層條目附上 ``(R<round> <speaker>)`` 錨點；無對應 pair 者原樣保留。

    以「用已知 pair 重建 ``f'{s} {verb} {t}'`` 與條目精確相等」配對，避免 split 解析在
    角色名含空白時誤判。錨點只在規則條目真有 transcript 來源時才加——絕不憑空捏造。
    """
    out: list[str] = []
    for e in entries:
        suffix = ""
        for (s, t), r in pair_rounds.items():
            if e == f"{s} {verb} {t}":
                suffix = f" (R{r} {s})"
                break
        out.append(f"{e}{suffix}")
    return out


def _anchored_from_summary(summary: dict, key: str, transcript: list[Utterance]) -> list[str]:
    """取 summary[key] 的規則條目並附 (round, speaker) 錨點。"""
    return _anchor_list(list(summary.get(key) or []), _VERB[key], _pair_rounds(transcript))


# ── (round, speaker) 錨點護欄（任務 #2）────────────────────────────────────────
#
# 對 senior **自產**（非由 `_anchored_from_summary` 回填）的非空鍵條目做保守查驗：
# 抽 `(R<n> <speaker>)` token、驗該 speaker 是否真的在 transcript 出現過；抽不到錨點、
# 或 speaker 不存在於 transcript 者，加 `（未錨定）` 後綴——讓「LLM 自填」與「有
# transcript 來源」在 CONCLUSION.md 上可視區分。
#
# 已知限制（設計決策，誠實暴露）：本護欄僅「盡力而為」——只驗 speaker 是否出現，
#   **不驗** round 是否吻合、**不驗** 該條論點是否真對應 transcript 的某 pair。
#   故「真 speaker＋幻覺論點」（LLM 借真名編造未發生的論點）仍會通過、不被標記。
#   護欄是可視化輔助、非幻覺攔截保證。`re` 僅用 ERE 等價語法（無 PCRE/lookbehind），
#   符合 CLAUDE.md 可攜性鐵則。
#
# 保守策略：寧漏標不誤傷——一條含多個錨點時，只要任一錨點的 speaker 屬實即視為已錨定。

_ANCHOR_RE = re.compile(r"\(R\d+ ([^)]+)\)")
_UNANCHORED = "（未錨定）"


def _transcript_speakers(transcript: list[Utterance]) -> set[str]:
    """transcript 中真實出現過的 speaker 集合（錨點 speaker 的有效性以此為準）。"""
    return {u.speaker for u in transcript}


def _guard_anchor(entry: str, speakers: set[str]) -> str:
    """對單條 LLM 自產條目查驗錨點；無有效錨點則加 `（未錨定）`。

    有效＝條目含至少一個 `(R<n> <speaker>)` 且該 speaker 確在 transcript 出現。
    已標 `（未錨定）` 者不重複標（冪等）。
    """
    if entry.endswith(_UNANCHORED):
        return entry
    for m in _ANCHOR_RE.finditer(entry):
        if m.group(1).strip() in speakers:
            return entry
    return f"{entry}{_UNANCHORED}"


def _guard_list(entries: list[str], transcript: list[Utterance]) -> list[str]:
    """對一串 LLM 自產條目逐條套錨點護欄。"""
    speakers = _transcript_speakers(transcript)
    return [_guard_anchor(e, speakers) for e in entries]


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
        "④ 自我校驗：逐條檢查每條結論是否都能對應上方骨架的某 (round, speaker)；"
        "能對應者帶上該錨點（例如「(R2 engineer)」），查無骨架依據者一律刪除、不得保留。\n"
        "（錨點一律取自上方骨架，不得自行杜撰 round 或 speaker；"
        "有依據才留、留則帶錨——這是一致準則，不是「盡量」。）\n"
    )


def _fallback_from_summary(summary: dict, transcript: list[Utterance]) -> dict[str, list[str]]:
    """蒸餾失靈時的降級結論：直接用規則式 summary 骨架填四鍵（帶 (round, speaker) 錨點）。

    consensus→共識、disagreements→分歧、open_questions→未決；行動段留空並標明
    蒸餾失靈（不以末輪發言冒充 action，設計決策）。仍回完整四鍵 dict，呼叫端可正常落盤。

    三段規則條目均附 transcript 來源錨點——確保 fallback 路徑產出的 CONCLUSION.md
    仍能「至少一條回指 transcript」（驗收 #5），不因走降級而失去可查證性（critic 退回點）。
    """
    return {
        "consensus": _anchored_from_summary(summary, "consensus", transcript),
        "disagreements": _anchored_from_summary(summary, "disagreements", transcript),
        "open_questions": _anchored_from_summary(summary, "open_questions", transcript),
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
    :param transcript: 該場 ``Utterance`` 清單（取輪數，並作為 (round, speaker) 錨點來源）。
    :returns: ``{"consensus", "disagreements", "open_questions", "actions"}`` 四鍵 list dict。
    """
    prompt = build_prompt(summary, transcript)
    distilled = await senior.speak(prompt, broadcast)
    parsed = flow.parse_conclusion(distilled or "")
    if _is_empty(parsed):
        return _fallback_from_summary(summary, transcript)
    # 部分漏標：senior 標了某些前綴卻漏了別的（如只給 `行動:`）。此時規則層已知為真的
    # consensus/disagreements/open_questions 不可被靜默丟棄——空鍵以規則骨架回填（帶
    # 來源錨點），比整碗接受 LLM 殘缺輸出更穩（高工建議）。actions 規則層無對應來源，照
    # LLM 輸出。回填用的是帶 (round, speaker) 錨點的規則條目——錨點來源為 transcript 事實，
    # 不信任 LLM 自填（設計決策）；LLM 自行產出的非空鍵則維持其原文不強加錨點。
    # 護欄判別以「是否走 _anchored_from_summary 回填」為界（設計決策）：
    #   - 空鍵 → 以規則骨架回填，錨點來自 transcript 事實，不過護欄（不可能未錨定）。
    #   - 非空鍵 → senior 自產，過護欄：無有效 (round, speaker) 錨點者標 （未錨定）。
    for key in ("consensus", "disagreements", "open_questions"):
        if not parsed.get(key):
            parsed[key] = _anchored_from_summary(summary, key, transcript)
        else:
            parsed[key] = _guard_list(parsed[key], transcript)
    # actions 永遠走 LLM 原文、不在回填範圍內（設計決策明確涵蓋），一律過護欄。
    parsed["actions"] = _guard_list(parsed.get("actions") or [], transcript)
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


def _json_path(cwd: Path) -> Path:
    return Path(cwd) / "conclusion.json"


# sidecar schema 版本——供 M2 歷史回顧/自我演進辨識格式演進（設計決策）。
_SIDECAR_VERSION = 1


def _write_sidecar(
    cwd: Path, conclusion: dict[str, list[str]], session_id: str, rounds: int
) -> None:
    """best-effort 雙寫機讀 conclusion.json（四鍵＋version＋session_id＋rounds）。

    沿用 adr.py 的 atomic tmp-replace。失敗時清理 .json.tmp 殘留並 log warning、**不拋例外**
    ——md 為人讀主檔/驗收核心已先落保底，sidecar 為 M2 前瞻附屬，異常時可降級為只保 md。
    """
    data = {
        "version": _SIDECAR_VERSION,
        "session_id": session_id,
        "rounds": rounds,
        "consensus": list(conclusion.get("consensus") or []),
        "disagreements": list(conclusion.get("disagreements") or []),
        "open_questions": list(conclusion.get("open_questions") or []),
        "actions": list(conclusion.get("actions") or []),
    }
    tmp = _json_path(cwd).with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(_json_path(cwd))
    except OSError:
        log.warning("conclusion.json sidecar 寫入失敗，降級為只保 CONCLUSION.md", exc_info=True)
        # 清理可能殘留的 .json.tmp，避免 workspace 留未追蹤殘檔（CLAUDE.md 鐵則）。
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)


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
    rounds: int = 0,
) -> Path | None:
    """把結論 dict 渲染成 ``CONCLUSION.md`` 落 workspace 根（覆寫式單檔），回傳路徑。

    雙寫（任務 #3）：``CONCLUSION.md`` 人讀主檔 ＋ ``conclusion.json`` 機讀 sidecar
    （供 M2 歷史回顧/自我演進）。語義為「主檔先寫保底、sidecar best-effort 後寫」——
    sidecar 寫入失敗只降級為保留 md ＋ log warning，不拋例外、不影響回傳（設計決策）。

    沿用 ``adr.py`` 的 atomic tmp-replace 寫入（避免半截檔）。每場覆寫——結論是本場快照，
    歷史保存靠 git commit（orchestrator 接線），非 append 累積。

    ``cwd`` 為 None（無 workspace 的單元測試）時兩檔皆不落、回 None；其餘永遠產出 md
    （即便四鍵皆空也寫出四段骨架，確保 fallback 路徑仍有 CONCLUSION.md，驗收 #6）。

    :param conclusion: :func:`summarize` 回傳的四鍵 dict（已含 fallback 處理）。
    :param session_id: 寫入 sidecar、並供呼叫端紀錄/事件用，md 落盤本身不依賴。
    :param rounds: 該場討論輪數，寫入 sidecar（人讀 md 不含此欄）。
    :returns: 寫出的 ``CONCLUSION.md`` 路徑，或 ``cwd is None`` 時 ``None``。
    """
    if cwd is None:
        return None
    # 主檔先落保底：md 為驗收核心（#3/#5/#6），務必在 best-effort sidecar 之前完成。
    path = _md_path(cwd)
    text = render_markdown(conclusion or {})
    tmp = path.with_suffix(".md.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
    # 機讀 sidecar best-effort 後寫：失敗不拖垮主檔與回傳。
    _write_sidecar(cwd, conclusion or {}, session_id, rounds)
    return path
