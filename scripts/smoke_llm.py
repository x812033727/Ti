#!/usr/bin/env python3
"""scripts/smoke_llm.py — 可重複的 LLM 冒煙腳本（任務 #1 骨架）。

對 :class:`studio.discussion.DiscussionEngine` 跑一場多角色討論，支援兩種發言調度
模式、可調併發與輪數，並具 ``--offline`` stub 模式：

- ``--mode round_robin|parallel``：同 engine 的兩種調度。
- ``--concurrency N``：parallel 模式映射為 ``asyncio.Semaphore(N)``，注入 engine 既有
  semaphore 注入點；round_robin 為序列發言，併發旗標僅記錄不生效。
- ``--rounds N``：討論輪數（映射 engine ``max_rounds``）。
- ``--offline``：注入輕量 :class:`StubExpert`（腳本化假回應，含合法 ``回應 @角色名:``
  引用），**全程不打 ``api.anthropic.com``**，用於無 key／無外網時驗證流程骨架。

本腳本是**純編排消費端**：只調用 engine 與其公開資料結構，不複製或修改
discussion/experts 核心邏輯（守架構決策）。@引用遵循度量測（任務 #2）直接統計
engine 已用 ``parse_mentions`` 解析好的 ``Utterance.mentions``，不另寫平行 parser；
共識判定沿用 ``build_summary`` 輸出，明確區分「全員無反對」與「強共識」。

真實 API 面（去掉 ``--offline``）需在具 ANTHROPIC 金鑰＋外網的環境另跑——本 sandbox
網路白名單不含 ``api.anthropic.com``，真實請求在此打不通，故預設 demo 走 ``--offline``，
真實面為明示移交待辦。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# 從 scripts/ 執行時，確保 repo 根（其下有 studio/ 套件）可被 import。
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from studio import roles  # noqa: E402
from studio.discussion import DiscussionEngine, DiscussionResult  # noqa: E402
from studio.experts import (  # noqa: E402
    API_ERROR_FALLBACK_MARKER,
    RATE_LIMIT_FALLBACK_MARKER,
)

# 報告預設輸出路徑（驗收 #5）。
DEFAULT_REPORT_PATH = _REPO_ROOT / "docs" / "SMOKE_REPORT.md"

# 預設參與討論的角色（key）：取一組有代表性的角色跑冒煙。
DEFAULT_ROLE_KEYS = ("pm", "engineer", "senior", "researcher")

# 冒煙議題：足夠具體讓角色能彼此引用、表態。
SMOKE_TOPIC = (
    "我們要為多角色討論引擎做一次上線前的健全度評估：\n"
    "請各自指出當前最該優先處理的風險點，並對其他人的提案明確表態（同意或反對）。"
)


class StubExpert:
    """離線冒煙用的輕量假專家，實作 ``async speak(prompt, broadcast) -> str`` 協議。

    產生符合 ``回應 @角色名: 同意|反對 ＋理由`` 結構化格式的台詞（引用一位 peer），
    讓 :func:`studio.discussion.parse_mentions` 能解析出合法 Mention，使後續 @遵循度
    量測有意義。**不呼叫任何 LLM、不發任何網路請求**。

    - ``dissent=True``：發「反對」（驅動「有反對」劇本）；``False``：發「同意」
      （驅動「全員無反對」劇本）。
    - 首輪開場（prompt 不含「上一輪全員發言」段）只表態、不引用，貼合真實情境
      「第一位發言者無前文可回應」——這也讓遵循率分母可正確排除開場。
    """

    def __init__(self, name: str, peers: list[str], dissent: bool = False):
        self.name = name
        self._peers = [p for p in peers if p != name]
        self._dissent = dissent
        self.calls = 0  # 已發言次數（供測試/觀測）

    async def speak(self, prompt: str, broadcast) -> str:  # noqa: ANN001 - 協議 duck-typed
        self.calls += 1
        # engine 注入的 broadcast 在離線預設是 no-op；冒煙不需要真的等待節奏。
        is_opening = "【上一輪全員發言】" not in prompt
        stance = "反對" if self._dissent else "同意"
        if is_opening or not self._peers:
            return (
                f"{self.name} 開場意見：先界定問題邊界，我認為最該優先處理的是"
                "流程骨架在限流/錯誤下的韌性。"
            )
        # 引用一位 peer（依發言次數輪替），輸出嚴格符合結構化引用格式。
        target = self._peers[(self.calls - 1) % len(self._peers)]
        return (
            f"{self.name} 第 {self.calls} 次發言，補一個可挑戰點：先驗證再放行。\n"
            f"回應 @{target}: {stance} 理由：其提案在限流路徑上仍有未覆蓋的失敗分支。"
        )

    async def stop(self) -> None:  # 與真實 Expert 介面對齊（engine 不強制呼叫）。
        return None


def _participant_names(role_keys: tuple[str, ...]) -> list[str]:
    """把角色 key 轉成 participant 名稱（即 role.name），順序即發言順序。"""
    return [roles.BY_KEY[k].name for k in role_keys]


def build_offline_participants(
    role_keys: tuple[str, ...] = DEFAULT_ROLE_KEYS, dissent: bool = False
) -> list[tuple[str, StubExpert]]:
    """建構離線 stub 參與者清單。

    ``dissent=True`` 時讓最後一位角色發「反對」（其餘同意），用於展示「有反對」劇本；
    ``False`` 時全員同意，展示「全員無反對」。
    """
    names = _participant_names(role_keys)
    dissenters = {names[-1]} if dissent and names else set()
    return [(n, StubExpert(n, names, dissent=n in dissenters)) for n in names]


def build_real_participants(role_keys: tuple[str, ...] = DEFAULT_ROLE_KEYS):
    """建構真實 Claude 參與者清單（非離線路徑）。

    需具 ANTHROPIC 金鑰＋外網；本 sandbox 連不到 ``api.anthropic.com``，故此路徑僅在
    真實環境可跑（移交待辦）。每位專家為獨立 :class:`studio.experts.Expert`。
    """
    import tempfile

    from studio.experts import Expert

    session_id = "smoke-llm"
    cwd = Path(tempfile.mkdtemp(prefix="ti-smoke-"))
    return [(roles.BY_KEY[k].name, Expert(roles.BY_KEY[k], session_id, cwd)) for k in role_keys]


async def run_smoke(
    mode: str,
    concurrency: int,
    rounds: int,
    offline: bool,
    dissent: bool = False,
) -> DiscussionResult:
    """跑一場冒煙討論並回傳 :class:`DiscussionResult`。"""
    if offline:
        participants = build_offline_participants(dissent=dissent)
    else:
        participants = build_real_participants()

    # parallel 模式才需要 semaphore 節流；round_robin 為序列發言。
    semaphore = asyncio.Semaphore(concurrency) if mode == "parallel" else None
    engine = DiscussionEngine(
        participants,
        mode=mode,
        max_rounds=rounds,
        semaphore=semaphore,
    )
    return await engine.run(SMOKE_TOPIC)


def _round1_first_speaker(transcript: list) -> str | None:  # noqa: ANN001 - list[Utterance]
    """回傳首輪第一位發言者名稱（transcript 依 participants 順序），無則 None。"""
    return next((u.speaker for u in transcript if u.round == 1), None)


def _is_opener(mode: str, utterance, round1_first_speaker: str | None) -> bool:  # noqa: ANN001
    """判定某發言是否為「結構上不可能引用」的開場發言（遵循率分母須排除）。

    依 engine 的 prompt 組裝規則（``DiscussionEngine._build_prompt``）逆推：

    - ``parallel``：首輪全員皆基於**空的 prev_round 快照**發言，prompt 無「上一輪全員
      發言」段——整個首輪都無前文可引用，全數視為開場。
    - ``round_robin``：同輪後者可見同輪前者（engine 傳 ``prev_round + this_round``），
      故僅**首輪第一位**發言者無任何前文，只有他算開場。
    """
    if utterance.round != 1:
        return False
    if mode == "parallel":
        return True
    return utterance.speaker == round1_first_speaker


def measure_mention_adherence(result: DiscussionResult, mode: str) -> dict:
    """量測 @引用格式遵循度（程式化、零 LLM）。

    **複用 engine 既有 ``parse_mentions`` 的結果**：每筆 ``Utterance.mentions`` 即
    ``discussion.parse_mentions`` 在 ``run()`` 內解析所得（白名單交替、立場二值、丟棄
    自我引用），本函式不另寫一套解析，只統計。

    遵循率定義（架構決策）：
        ``遵循率 = 產生 ≥1 條合法 Mention 的 utterance 數 / 應回應 utterance 數``
    分母**排除結構上不可能引用的開場發言**（見 :func:`_is_opener`），避免虛壓數字。

    回傳含 overall 與 per-round 細目、被排除開場數、合法 Mention 總數。
    """
    transcript = result.transcript
    first_speaker = _round1_first_speaker(transcript)
    per_round: dict[int, dict[str, int]] = {}
    eligible_total = 0
    compliant_total = 0
    excluded_openers = 0
    for u in transcript:
        if _is_opener(mode, u, first_speaker):
            excluded_openers += 1
            continue
        bucket = per_round.setdefault(u.round, {"eligible": 0, "compliant": 0, "mentions": 0})
        bucket["eligible"] += 1
        eligible_total += 1
        if u.mentions:  # parse_mentions 解析出 ≥1 條合法結構化引用
            bucket["compliant"] += 1
            compliant_total += 1
        bucket["mentions"] += len(u.mentions)

    rounds = []
    for rnd in sorted(per_round):
        b = per_round[rnd]
        rate = b["compliant"] / b["eligible"] if b["eligible"] else None
        rounds.append(
            {
                "round": rnd,
                "eligible": b["eligible"],
                "compliant": b["compliant"],
                "mentions": b["mentions"],
                "rate": rate,
            }
        )
    overall = compliant_total / eligible_total if eligible_total else None
    return {
        "mode": mode,
        "overall_rate": overall,
        "eligible_total": eligible_total,
        "compliant_total": compliant_total,
        "excluded_openers": excluded_openers,
        "total_mentions": sum(len(u.mentions) for u in transcript),
        "per_round": rounds,
    }


def classify_consensus(summary: dict) -> dict:
    """依 ``build_summary`` 的 consensus／disagreements 分類共識狀態，
    **明確區分「全員無反對」與「強共識」，不誤判**（驗收 #3）。

    - 有反對（``disagreements`` 非空）→ ``"有反對"``。
    - 無反對且有明確同意（``consensus`` 非空）→ 強共識。
    - 無反對但**無**明確同意 → 「全員無反對」僅為弱訊號，**不可**誤判為強共識
      （可能只是無人引用／全是開場），如實標註。
    """
    consensus = summary.get("consensus") or []
    disagreements = summary.get("disagreements") or []
    no_dissent = len(disagreements) == 0
    has_agreement = len(consensus) > 0
    if not no_dissent:
        label = "有反對"
    elif has_agreement:
        label = "全員無反對＋有明確同意（強共識）"
    else:
        label = "全員無反對但無明確同意（弱訊號，不可誤判為強共識）"
    return {
        "label": label,
        "no_dissent": no_dissent,
        "has_explicit_agreement": has_agreement,
        "is_strong_consensus": no_dissent and has_agreement,
        "consensus_count": len(consensus),
        "disagreement_count": len(disagreements),
    }


def count_failure_fallbacks(result: DiscussionResult) -> dict:
    """純消費端統計失敗 fallback 命中數——掃 transcript 找 ``experts.py`` 寫入的
    系統 fallback 文字標記（單一事實來源常數，import 自 experts，避免字串漂移）。

    - ``rate_limit_hits``：429 退避重試耗盡後落入 transcript 的 fallback 筆數。
    - ``api_error_hits``：SDK 錯誤文字命中後走 fallback 的筆數（架構決策：與 429
      為兩條獨立失敗路徑，各自獨立 counter，不混入同一計數器）。

    離線 stub 路徑不經 ``experts.py`` 真實失敗處理，故此兩數恆為 0——報告據此如實
    註記「離線未觸發」，不粉飾。

    比對**錨定 ``【系統】`` 前綴**（``_fallback_note`` 寫入的系統文字一律以此開頭）再
    配對標記，避免專家原文引用「因 API 限流（429）」等句被誤計（真實面假綠歸零）。
    """
    rate_limit_hits = 0
    api_error_hits = 0
    for u in result.transcript:
        text = u.text or ""
        if not text.startswith("【系統】"):
            continue
        if RATE_LIMIT_FALLBACK_MARKER in text:
            rate_limit_hits += 1
        if API_ERROR_FALLBACK_MARKER in text:
            api_error_hits += 1
    return {"rate_limit_hits": rate_limit_hits, "api_error_hits": api_error_hits}


def collect_run(
    mode: str, concurrency: int, rounds: int, offline: bool, dissent: bool, result: DiscussionResult
) -> dict:
    """把單次冒煙 run 的所有報告所需數據彙整成一個 dict（純量測，零 LLM）。"""
    return {
        "mode": mode,
        "concurrency": concurrency,
        "rounds": rounds,
        "offline": offline,
        "dissent": dissent,
        "stop_reason": result.stop_reason,
        "transcript": result.transcript,
        "adherence": measure_mention_adherence(result, mode),
        "consensus": classify_consensus(result.summary),
        "summary": result.summary,
        "failures": count_failure_fallbacks(result),
    }


def _fmt_rate(rate: float | None) -> str:
    """把遵循率格式化為百分比字串；分母為 0（無應回應發言）標 N/A。"""
    return "N/A（無應回應發言）" if rate is None else f"{rate * 100:.1f}%"


# 發言品質抽樣的顯示上限（表頭會如實標註「顯示前 N/總數 筆」，避免讀者誤判覆蓋全量）。
_SAMPLE_LIMIT = 6


def _sample_lines(transcript: list, limit: int = _SAMPLE_LIMIT) -> list[str]:  # noqa: ANN001
    """從 transcript 取樣若干筆發言，回傳 markdown 表格列（首行＋mention 數）。"""
    rows = []
    for u in transcript[:limit]:
        head = (u.text.splitlines()[0] if u.text else "").replace("|", "\\|")
        if len(head) > 70:
            head = head[:70] + "…"
        rows.append(f"| R{u.round} | {u.speaker} | {len(u.mentions)} | {head} |")
    return rows


def _render_run_block(run: dict) -> list[str]:
    """渲染單一 mode 的子區塊（發言品質抽樣＋@遵循率＋共識判定）。"""
    m = run["adherence"]
    total = len(run["transcript"])
    shown = min(_SAMPLE_LIMIT, total)
    sample_note = (
        f"（首行＋解析出的合法 Mention 數；顯示前 {shown}/{total} 筆"
        f"{'，已截斷' if total > shown else ''}）"
    )
    lines = [
        f"### mode = `{run['mode']}`"
        f"（rounds={run['rounds']}, concurrency={run['concurrency']}"
        f"{'，僅 parallel 生效' if run['mode'] == 'round_robin' else ''}）",
        "",
        f"- 發言總數：{total}；stop_reason：`{run['stop_reason']}`",
        "",
        f"**發言品質抽樣**{sample_note}：",
        "",
        "| 輪 | 發言者 | mentions | 首行摘要 |",
        "| --- | --- | --- | --- |",
        *_sample_lines(run["transcript"]),
        "",
        "**@引用格式遵循率**（複用 `discussion.parse_mentions` 解析結果，非另寫一套）：",
        "",
        f"- 整體遵循率：**{_fmt_rate(m['overall_rate'])}**"
        f"（{m['compliant_total']}/{m['eligible_total']} 應回應發言；"
        f"已排除 {m['excluded_openers']} 筆結構上不可能引用的開場發言）",
        f"- 合法 Mention 總數：{m['total_mentions']}",
    ]
    for r in m["per_round"]:
        lines.append(
            f"  - R{r['round']}：{_fmt_rate(r['rate'])}"
            f"（{r['compliant']}/{r['eligible']}，mentions={r['mentions']}）"
        )
    c = run["consensus"]
    lines += [
        "",
        "**共識判定**（區分「全員無反對」與「強共識」，不誤判）：",
        "",
        f"- 分類：**{c['label']}**",
        f"- `no_dissent={c['no_dissent']}` `has_explicit_agreement="
        f"{c['has_explicit_agreement']}` `is_strong_consensus={c['is_strong_consensus']}`"
        f"（同意 {c['consensus_count']} 條／反對 {c['disagreement_count']} 條）",
        "",
    ]
    return lines


def render_report(runs: list[dict], *, offline: bool) -> str:
    """把多個 mode 的 run 數據渲染成 `SMOKE_REPORT.md`（固定四段，如實不粉飾）。

    四段（驗收 #5）：①發言品質抽樣 ②@遵循率 ③rate limit 行為 ④SDK 錯誤文字命中數。
    ``offline=True`` 時強制輸出「未涵蓋真實 API 面為移交待辦」段落（驗收 #6）。
    """
    api_mode = "離線 stub（`--offline`）" if offline else "真實 Claude API"
    lines = [
        "# 冒煙驗證報告（SMOKE_REPORT.md）",
        "",
        "> 由 `scripts/smoke_llm.py --report` 自動產出。本報告如實記錄抽樣與量測結果，"
        "不粉飾；數值對應本次執行的 transcript，可回溯。",
        "",
        f"- **本次是否用真實 API**：{api_mode}",
        "- 涵蓋模式：" + "、".join(f"`{r['mode']}`" for r in runs),
        "",
    ]

    # ── 第一段：發言品質抽樣 + 第二段：@遵循率（按 mode 分塊呈現） ──
    lines += ["## 一、發言品質抽樣 ＆ 二、@引用遵循率（按模式）", ""]
    for run in runs:
        lines += _render_run_block(run)

    # ── 第三段：rate limit 行為 ──
    lines += ["## 三、rate limit（429）行為", ""]
    if offline:
        lines += [
            "- **離線未觸發**：`--offline` 注入 `StubExpert`，發言不經 `studio/experts.py` "
            "真實 API 路徑，全程不對 `api.anthropic.com` 發請求，故**未觸發任何 429**。",
            "- 「在第幾併發撞 429」：**N/A（離線未觸發）**。各 mode 併發設定如下，僅供真實面"
            "回歸時對照：",
        ]
        for run in runs:
            note = (
                "序列發言、併發旗標不生效" if run["mode"] == "round_robin" else "asyncio.Semaphore"
            )
            lines.append(f"  - `{run['mode']}`：concurrency={run['concurrency']}（{note}）")
    else:
        lines.append(
            "- 各 mode 的 429 退避 fallback 命中數（耗盡重試後落入 transcript 的系統文字）："
        )
        for run in runs:
            lines.append(
                f"  - `{run['mode']}`（concurrency={run['concurrency']}）："
                f"{run['failures']['rate_limit_hits']} 筆"
            )
    lines.append("")

    # ── 第四段：SDK 錯誤文字命中數 ──
    lines += [
        "## 四、SDK 錯誤文字命中數",
        "",
        "（指 SDK 把 API 錯誤塞進 `AssistantMessage` 文字、被 `experts.py` 偵測為該輪失敗"
        "走 fallback 的筆數；與 429 為兩條獨立 counter，不混計。）",
        "",
    ]
    if offline:
        lines.append("- **離線未觸發**：stub 不產生 SDK 錯誤文字，命中數恆為 **0**。")
        for run in runs:
            lines.append(f"  - `{run['mode']}`：{run['failures']['api_error_hits']} 筆")
    else:
        for run in runs:
            lines.append(f"- `{run['mode']}`：{run['failures']['api_error_hits']} 筆")
    lines.append("")

    # ── 誠實標註：真實 API 面移交待辦（offline 強制） ──
    if offline:
        lines += [
            "## 五、誠實標註：未涵蓋真實 API 面（移交待辦）",
            "",
            "本次為**離線 stub 跑**，**未涵蓋真實 API 面**，以下為明示移交待辦：",
            "",
            "- 真實 429 行為、實際撞限流的併發臨界點，**本次未驗**——本 sandbox 網路白名單"
            "不含 `api.anthropic.com`，真實請求打不通。",
            "- SDK 把錯誤塞進 `AssistantMessage` 文字的真實樣態，**本次未驗**（防線已有單元"
            "測試覆蓋，但非真實鏈路）。",
            "- 補驗方式：在具 ANTHROPIC 金鑰＋外網的環境，去掉 `--offline` 重跑"
            "`--mode round_robin` 與 `--mode parallel`（建議併發由小漸增），再以本腳本"
            "`--report` 產出真實面報告比對。",
            "",
        ]
    return "\n".join(lines)


def write_report(runs: list[dict], path: Path, *, offline: bool) -> Path:
    """渲染並寫出報告，回傳路徑。"""
    content = render_report(runs, offline=offline)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


async def generate_report(
    path: Path, *, concurrency: int, rounds: int, offline: bool, dissent: bool
) -> Path:
    """跑 round_robin 與 parallel 兩模式各一次，彙整後寫出報告（驗收 #5）。"""
    runs = []
    for mode in ("round_robin", "parallel"):
        result = await run_smoke(
            mode=mode, concurrency=concurrency, rounds=rounds, offline=offline, dissent=dissent
        )
        runs.append(collect_run(mode, concurrency, rounds, offline, dissent, result))
    return write_report(runs, path, offline=offline)


def _print_summary(result: DiscussionResult, *, mode: str, offline: bool) -> None:
    """把結果摘要與 @遵循度量測印到 stdout。"""
    print(f"=== 冒煙結果（mode={mode}, offline={offline}）===")
    print(f"transcript 發言數: {len(result.transcript)}")
    print(f"stop_reason: {result.stop_reason}")
    print("--- 發言概覽 ---")
    for u in result.transcript:
        head = u.text.splitlines()[0] if u.text else ""
        print(f"  [R{u.round}] {u.speaker}: mentions={len(u.mentions)} | {head[:60]}")

    metrics = measure_mention_adherence(result, mode)
    print("--- @引用格式遵循度（複用 parse_mentions 結果）---")
    print(f"  合法 Mention 總數: {metrics['total_mentions']}")
    print(
        f"  整體遵循率: {_fmt_rate(metrics['overall_rate'])} "
        f"（{metrics['compliant_total']}/{metrics['eligible_total']} 應回應發言；"
        f"已排除 {metrics['excluded_openers']} 筆開場發言）"
    )
    for r in metrics["per_round"]:
        print(
            f"    R{r['round']}: {_fmt_rate(r['rate'])} "
            f"（{r['compliant']}/{r['eligible']}，mentions={r['mentions']}）"
        )

    consensus = classify_consensus(result.summary)
    print("--- 共識判定（區分全員無反對 vs 強共識）---")
    print(f"  分類: {consensus['label']}")
    print(
        f"  no_dissent={consensus['no_dissent']} "
        f"has_explicit_agreement={consensus['has_explicit_agreement']} "
        f"is_strong_consensus={consensus['is_strong_consensus']}"
    )
    print(f"  consensus: {result.summary.get('consensus')}")
    print(f"  disagreements: {result.summary.get('disagreements')}")


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="smoke_llm.py",
        description="多角色討論引擎冒煙腳本（支援 round_robin/parallel 與 --offline stub）。",
    )
    p.add_argument(
        "--mode",
        choices=("round_robin", "parallel"),
        default="round_robin",
        help="發言調度模式（預設 round_robin）。",
    )
    p.add_argument(
        "--concurrency",
        type=int,
        default=2,
        help="parallel 模式的併發上限（映射 asyncio.Semaphore）；round_robin 不生效。",
    )
    p.add_argument("--rounds", type=int, default=2, help="討論輪數（映射 engine max_rounds）。")
    p.add_argument(
        "--offline",
        action="store_true",
        help="離線 stub 模式：用假回應驗流程骨架，全程不打 api.anthropic.com。",
    )
    p.add_argument(
        "--dissent",
        action="store_true",
        help="（離線）讓最後一位角色發反對，用於展示「有反對」劇本；預設全員同意。",
    )
    p.add_argument(
        "--report",
        nargs="?",
        const=str(DEFAULT_REPORT_PATH),
        default=None,
        metavar="PATH",
        help=(
            "產出結構化報告：跑 round_robin＋parallel 兩模式各一次，寫出四段 markdown "
            f"（預設 {DEFAULT_REPORT_PATH.relative_to(_REPO_ROOT)}）。指定此旗標時不印單次摘要。"
        ),
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    if args.concurrency < 1:
        print("錯誤：--concurrency 必須 ≥ 1", file=sys.stderr)
        return 2
    if args.rounds < 1:
        print("錯誤：--rounds 必須 ≥ 1", file=sys.stderr)
        return 2
    if args.report is not None:
        path = asyncio.run(
            generate_report(
                Path(args.report),
                concurrency=args.concurrency,
                rounds=args.rounds,
                offline=args.offline,
                dissent=args.dissent,
            )
        )
        print(f"已產出冒煙報告：{path}（mode=round_robin＋parallel, offline={args.offline}）")
        return 0
    result = asyncio.run(
        run_smoke(
            mode=args.mode,
            concurrency=args.concurrency,
            rounds=args.rounds,
            offline=args.offline,
            dissent=args.dissent,
        )
    )
    _print_summary(result, mode=args.mode, offline=args.offline)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
