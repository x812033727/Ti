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

import json
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING

from . import flow

if TYPE_CHECKING:  # 避免執行期循環匯入；型別僅供靜態檢查
    from .discussion import ExpertLike, Utterance

logger = logging.getLogger(__name__)


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


# ── 錨點程式化護欄（任務 #2）──────────────────────────────────────────────────
#
# 對 senior **自由文字自產**的條目（未走 `_anchored_from_summary` 回填者，含永遠走 LLM
# 的 actions 鍵）做保守驗證：抽 `(R<n> <speaker>)` token、驗 speaker 真的在 transcript
# 出現過，否則加 `（未錨定）` 後綴，使「LLM 自填」與「有 transcript 來源」在 CONCLUSION.md
# 上可視區分。
#
# 判別力上限（已知限制，誠實標明、非幻覺攔截保證）：本護欄只驗「speaker 是否出現」，
# **不驗 round 正確性、亦不驗論點是否真對應該 pair**——「真 speaker＋幻覺論點」仍會被
# 放行漏標。屬盡力而為，列為待辦。
#
# 為何保守：LLM 自由文字無法用規則層 `f"{s} {verb} {t}"` 精確重建，寧漏標不誤傷真錨點。
# `re` 僅用 ERE 等價語法（無 lookbehind/PCRE），符合 CLAUDE.md 可攜性鐵則。

_UNANCHORED_SUFFIX = "（未錨定）"

# 抽 `(R<數字> <speaker>)` 內的 speaker 片段；speaker 取到右括號前，容許含空白。
_ANCHOR_TOKEN_RE = re.compile(r"\(R\d+\s+([^()]+?)\)")


def _guard_anchor(entry: str, speakers: set[str]) -> str:
    """對 LLM 自產條目做保守錨點護欄：抽錨點 token、驗 speaker 存在；否則加 `（未錨定）`。

    已帶 `（未錨定）` 者不重複標。抽不到任何錨點 token，或抽到的 speaker 都不在 transcript
    speaker 集合中，才標未錨定（保守：寧漏標不誤傷真來源）。
    """
    if not entry or entry.rstrip().endswith(_UNANCHORED_SUFFIX):
        return entry
    for m in _ANCHOR_TOKEN_RE.finditer(entry):
        if m.group(1).strip() in speakers:
            return entry  # 有合法錨點且 speaker 存在 → 放行，不標
    return f"{entry}{_UNANCHORED_SUFFIX}"


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
        "④ 逐條自檢：輸出前再讀一遍每條結論，確認它都能對應上方骨架的某 (round, speaker)；"
        "有依據才保留、查無依據者刪除，不得保留任何骨架找不到出處的結論。\n"
        "每條保留下來的結論，能對應骨架錨點者帶上 (round, speaker)，例如「(R2 engineer)」，"
        "錨點須取自上方骨架——有依據才留、留則帶錨。\n"
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
    # 護欄 speaker 集合＝transcript 真實出現過的發言者（事實來源，不賭 LLM）。
    speakers = {u.speaker for u in transcript}
    for key in ("consensus", "disagreements", "open_questions"):
        if not parsed.get(key):
            # 空鍵以規則骨架回填——這些是規則層事實、錨點由 _anchor_list 程式產生，
            # 不過護欄（不會誤傷真來源）。
            parsed[key] = _anchored_from_summary(summary, key, transcript)
        else:
            # LLM 自產非空鍵 → 過護欄，對不上 transcript speaker 者標 `（未錨定）`。
            parsed[key] = [_guard_anchor(e, speakers) for e in parsed[key]]
    # actions 永遠走 LLM 原文、不在回填範圍 → 一律過護欄（設計決策：避免幻覺 action 漏網）。
    parsed["actions"] = [_guard_anchor(e, speakers) for e in (parsed.get("actions") or [])]
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


# 機讀 sidecar schema 版本——供 M2 歷史回顧/自我演進辨識結構演進。
_SIDECAR_VERSION = 1


def _write_sidecar(
    cwd: Path, conclusion: dict[str, list[str]], *, session_id: str, rounds: int
) -> None:
    """best-effort 雙寫機讀 ``conclusion.json``（沿用 atomic tmp-replace）。

    md 主檔已先落盤保底；sidecar 為 M2 前瞻附屬，寫入失敗只降級為「僅保留 CONCLUSION.md」
    ＋ log warning，不拋例外、不拖垮人讀主檔與既有 record→commit→broadcast 時序。
    失敗時清理殘留 ``.json.tmp``，避免 workspace 留未追蹤殘檔（CLAUDE.md 鐵則）。
    """
    payload = {
        "version": _SIDECAR_VERSION,
        "session_id": session_id,
        "rounds": rounds,
        **{key: list(conclusion.get(key) or []) for key in _KEYS},
    }
    jpath = _json_path(cwd)
    jtmp = jpath.with_suffix(".json.tmp")
    try:
        jtmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        jtmp.replace(jpath)
    # 名副其實的 best-effort：除 OSError（IO/磁碟）外，也涵蓋序列化錯誤（TypeError/
    # ValueError，如 payload 含非 JSON 值）——附屬 sidecar 任何失敗都降級，絕不向上拋出
    # 拖垮 record→commit→broadcast 時序（高工建議）。
    except (OSError, TypeError, ValueError) as exc:  # 失敗：降級保留 md，清理殘留 tmp
        logger.warning("conclusion.json sidecar 寫入失敗，降級為僅保留 CONCLUSION.md：%s", exc)
        try:
            jtmp.unlink(missing_ok=True)
        except OSError:
            pass


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

    沿用 ``adr.py`` 的 atomic tmp-replace 寫入（避免半截檔）。每場覆寫——結論是本場快照，
    歷史保存靠 git commit（orchestrator #5 接線），非 append 累積。

    雙寫機讀 sidecar ``conclusion.json``（任務 #3）：md 主檔**先落盤保底**，sidecar 為
    best-effort 後寫（見 :func:`_write_sidecar`），失敗只降級保留 md、不影響回傳與時序。
    sidecar 與 md 同場覆寫、同由 orchestrator commit（``git add -A``）一併入 git（#4）。

    ``cwd`` 為 None（無 workspace 的單元測試）時兩檔皆不落、回 None；其餘永遠產出 md
    （即便四鍵皆空也寫出四段骨架，確保 fallback 路徑仍有 CONCLUSION.md，驗收 #6）。

    :param conclusion: :func:`summarize` 回傳的四鍵 dict（已含 fallback 處理）。
    :param session_id: 寫入 sidecar、供 M2 機讀檢索；md 本身不依賴。
    :param rounds: 該場討論輪數，寫入 sidecar（人讀 md 不放）。
    :returns: 寫出的 ``CONCLUSION.md`` 路徑，或 ``cwd is None`` 時 ``None``。
    """
    if cwd is None:
        return None
    conclusion = conclusion or {}
    path = _md_path(cwd)
    text = render_markdown(conclusion)
    tmp = path.with_suffix(".md.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
    # md 主檔已落盤保底，再 best-effort 雙寫機讀 sidecar（失敗不拖垮主檔）。
    _write_sidecar(cwd, conclusion, session_id=session_id, rounds=rounds)
    return path
