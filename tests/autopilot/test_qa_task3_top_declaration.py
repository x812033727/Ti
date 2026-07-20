"""QA 任務 #3：頂部半閉環聲明收斂驗收測試。

對應驗收標準（任務 #3）：
  AC#3.1 聲明收斂為「v0.2.0 此鏈已生產閉環；後續版本仍半閉環、尚待逐版生產驗證」
  AC#3.2 完整保留六關鍵詞：`真實`／`tag-push`／`端到端`／`生產驗證`／`半閉環`／`尚待`
  AC#3.3 與邊界表兩列皆 ✅ 自洽，不宣稱 body 置頂「待封／範圍外」
  AC#3.4 未把『尚待／半閉環』整體軟化成『已完整』（守護自證漂移必翻紅）

設計沿用團隊硬規則：把每條契約抽成 `check_*` 判斷式，baseline 綠與 mutation 紅共用同一把尺，
證明守護有真鑑別力。成對黑樣本（竄改頂部聲明為「已完整」／抽掉任一關鍵詞／改邊界表為 ⏳）
任一都必須翻紅。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
HANDOFF_MD = ROOT / "docs" / "release-e2e-handoff.md"


@pytest.fixture(scope="module")
def handoff_text() -> str:
    assert HANDOFF_MD.exists(), f"前提失效：缺移交文件 {HANDOFF_MD}"
    return HANDOFF_MD.read_text(encoding="utf-8")


# 頂部半閉環聲明（最重要，先讀）段落切片：抓到 `## 半閉環聲明` 區塊的單行結論
# 不取整段，因為整段含解釋、舉例、換行；只取粗體收斂的那一句。
DECLARATION_LITERAL_FRAGMENTS = (
    "v0.2.0 此鏈已生產閉環",
    "後續版本仍半閉環",
    "尚待逐版生產驗證",
)

SIX_KEYWORDS = ("真實", "tag-push", "端到端", "生產驗證", "半閉環", "尚待")


def _declaration_paragraph(text: str) -> str:
    """抽出 `## 半閉環聲明` 段落開頭的粗體單行結論。

    以「## 半閉環聲明（最重要，先讀）」錨點起始，取下一個 `## ` 之前的中文粗體段。
    這條段是「v0.2.0 此鏈已生產閉環；後續版本仍半閉環、尚待逐版生產驗證」所在的單行收斂。
    """
    lines = text.splitlines()
    start = None
    for i, ln in enumerate(lines):
        if ln.startswith("## 半閉環聲明"):
            start = i + 1
            break
    assert start is not None, "前提失效：找不到『## 半閉環聲明』段落錨點"
    end = start
    for j in range(start, len(lines)):
        if lines[j].startswith("## "):
            end = j
            break
    else:
        end = len(lines)
    return "\n".join(lines[start:end])


def _norm_decl(text: str) -> str:
    """把斷行／多空白壓成單一半形空白後比對用。"""
    return re.sub(r"\s+", " ", text).strip()


def check_declaration_text(text: str) -> list[str]:
    """AC#3.1：頂部聲明必須含三段收斂字面片段（容忍斷行／多空白）。"""
    problems: list[str] = []
    decl = _norm_decl(_declaration_paragraph(text))
    for frag in DECLARATION_LITERAL_FRAGMENTS:
        if _norm_decl(frag) not in decl:
            problems.append(f"頂部聲明缺字面片段：{frag!r}（decl={decl[:200]!r}）")
    return problems


def check_six_keywords(text: str) -> list[str]:
    """AC#3.2：完整保留六關鍵詞。任一缺漏即紅（防止漂移為簡化版）。"""
    problems: list[str] = []
    for kw in SIX_KEYWORDS:
        if kw not in text:
            problems.append(f"頂部聲明缺關鍵詞：{kw!r}")
    return problems


def check_consistency_with_table(text: str) -> list[str]:
    """AC#3.3：聲明與邊界表兩列 ✅ 自洽，且聲明不再說 body 置頂『待封／範圍外』。"""
    problems: list[str] = []

    # 抓邊界表中「真實 ... body 頂部 Breaking 置頂」列與「release-smoke 觸發」列
    body_row = ""
    smoke_row = ""
    for line in text.splitlines():
        if line.lstrip().startswith("|"):
            if "body 頂部 Breaking 置頂" in line:
                body_row = line
            elif "release-smoke.yml" in line and "release: published" in line:
                smoke_row = line

    if not body_row:
        problems.append("邊界表缺『body 頂部 Breaking 置頂』列")
    if not smoke_row:
        problems.append("邊界表缺『release-smoke 觸發』列")
    if body_row and "✅" not in body_row:
        problems.append("body 置頂列未標 ✅（與頂部聲明自稱『已生產閉環』矛盾）")
    if smoke_row and "✅" not in smoke_row:
        problems.append("smoke 觸發列未標 ✅（與頂部聲明自稱『已生產閉環』矛盾）")
    if body_row and ("⏳" in body_row or "❌" in body_row):
        problems.append("body 置頂列仍含 ⏳/❌（未翻封）")
    if smoke_row and ("⏳" in smoke_row or "❌" in smoke_row):
        problems.append("smoke 觸發列仍含 ⏳/❌（未翻封）")

    # 反向：聲明不得說「待封／範圍外／尚未驗證」之類與 ✅ 矛盾的修飾
    forbidden_in_declaration = ("待封", "範圍外", "尚未驗證", "未驗證", "未閉環")
    decl = _declaration_paragraph(text)
    for w in forbidden_in_declaration:
        if w in decl:
            problems.append(f"頂部聲明仍含與 ✅ 矛盾的修飾詞：{w!r}")

    return problems


def check_no_full_softening(text: str) -> list[str]:
    """AC#3.4：未把『尚待／半閉環』整體軟化成『已完整』。"""
    problems: list[str] = []
    # baseline 必含「尚待」與「半閉環」
    if "尚待" not in text:
        problems.append("baseline 失效：原本就無『尚待』")
    if "半閉環" not in text:
        problems.append("baseline 失效：原本就無『半閉環』")
    # baseline 不得在『後續版本』段落把『半閉環／尚待』整段改為『已完整／已通過』之類
    decl = _declaration_paragraph(text)
    if re.search(r"後續版本.*已(完整|通過|閉環)", decl):
        problems.append("頂部聲明把『後續版本』段落軟化為『已完整／已通過』（不誠實漂移）")
    return problems


# ---------------------------------------------------------------------------
# Baseline 通過
# ---------------------------------------------------------------------------


def test_declaration_contains_three_literal_fragments(handoff_text):
    """AC#3.1：頂部聲明收斂為三段字面片段齊備。"""
    problems = check_declaration_text(handoff_text)
    assert problems == [], "頂部聲明收斂缺漏：\n  - " + "\n  - ".join(problems)


def test_declaration_contains_all_six_keywords(handoff_text):
    """AC#3.2：六關鍵詞全在（守護 baseline 防漂移為簡化版）。"""
    problems = check_six_keywords(handoff_text)
    assert problems == [], "六關鍵詞缺漏：\n  - " + "\n  - ".join(problems)


def test_declaration_consistent_with_table_two_green_rows(handoff_text):
    """AC#3.3：聲明與邊界表兩列 ✅ 自洽，不與 ✅ 矛盾。"""
    problems = check_consistency_with_table(handoff_text)
    assert problems == [], "自洽性破裂：\n  - " + "\n  - ".join(problems)


def test_declaration_not_fully_softened(handoff_text):
    """AC#3.4：未把『尚待／半閉環』整體軟化為『已完整』。"""
    problems = check_no_full_softening(handoff_text)
    assert problems == [], "頂部聲明被軟化：\n  - " + "\n  - ".join(problems)


# ---------------------------------------------------------------------------
# 成對黑樣本：任一 mutation 必須翻紅（自證判別力）
# ---------------------------------------------------------------------------


def test_mutation_soften_尚待_to_已完整_turns_red(handoff_text):
    """把『尚待逐版生產驗證』改為『已完整逐版生產驗證』→ 守護必翻紅。

    驗證 `check_no_full_softening` 的後續版本…已通過 regex 與 `check_six_keywords`
    的『尚待』缺漏。任一斷言未紅即代表守護對『已完整漂移』沒判別力。
    """
    mutated = handoff_text.replace("尚待逐版生產驗證", "已完整逐版生產驗證")
    assert mutated != handoff_text, "mutation 無效：未替換到『尚待逐版生產驗證』"
    p_keywords = check_six_keywords(mutated)
    p_softening = check_no_full_softening(mutated)
    problems = p_keywords + p_softening
    assert any("尚待" in p or "已(完整|通過|閉環)" in p or "軟化" in p for p in problems), (
        f"假綠：把『尚待』改為『已完整』後守護未翻紅，problems={problems}"
    )


def test_mutation_drop_v020_closure_turns_red(handoff_text):
    """把『v0.2.0 此鏈已生產閉環』整段刪掉 → 三段字面片段守護必翻紅。"""
    mutated = handoff_text.replace("v0.2.0 此鏈已生產閉環；", "")
    assert mutated != handoff_text, "mutation 無效：未替換到 v0.2.0 已生產閉環片段"
    problems = check_declaration_text(mutated)
    assert any("v0.2.0 此鏈已生產閉環" in p for p in problems), (
        f"假綠：刪掉 v0.2.0 收斂片段後守護未翻紅，problems={problems}"
    )


def test_mutation_drop_尚待_turns_red(handoff_text):
    """抽掉任一關鍵詞（『尚待』）→ 六關鍵詞守護必翻紅。"""
    mutated = handoff_text.replace("尚待逐版生產驗證", "逐版生產驗證")
    assert mutated != handoff_text, "mutation 無效：未替換到『尚待』"
    problems = check_six_keywords(mutated)
    assert any("尚待" in p for p in problems), (
        f"假綠：抽掉『尚待』後守護未翻紅，problems={problems}"
    )


def test_mutation_body_row_reverted_to_pending_turns_red(handoff_text):
    """邊界表 body 列被改回 ⏳ → 自洽性守護必翻紅（防『表未翻、頭已寫』矛盾中間態）。"""
    body_row = ""
    for line in handoff_text.splitlines():
        if line.lstrip().startswith("|") and "body 頂部 Breaking 置頂" in line:
            body_row = line
            break
    assert "✅" in body_row, "baseline 失效：body 列原本就無 ✅"
    mutated = handoff_text.replace(body_row, body_row.replace("✅", "⏳"))
    assert mutated != handoff_text, "mutation 無效：未改到 body 列 ✅"
    problems = check_consistency_with_table(mutated)
    assert any("body 置頂列" in p for p in problems), (
        f"假綠：body 列改 ⏳ 後自洽性守護未翻紅，problems={problems}"
    )
