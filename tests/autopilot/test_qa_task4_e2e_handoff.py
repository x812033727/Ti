"""QA 任務 #4：Release E2E 移交待辦文件守護測試。

守護 `docs/release-e2e-handoff.md`（本輪新增的移交明文），對應驗收標準 AC#5：

  1. 明文標註「真實 `v*` tag-push 端到端仍為半閉環，尚待生產驗證」——不以測試綠冒充 E2E。
  2. 列出**發佈後**具名人工核對步驟：在 GitHub release 頁核對 body 頂部即
     `## ⚠️ Breaking Changes`、含四要素與 `TI_REQUIRE_CHOWN=warn/off` 逃生艙。
  3. 勾稽已閉環的具名守護測試（pretag 兩出口），把「已閉環 vs 未閉環」邊界講清楚。

設計（沿用團隊硬規則：自證對應 + 黑樣本）：
  - 每條契約抽成 `check_*` 判斷式，baseline 綠與 mutation 紅共用同一把尺，
    證明守護有真鑑別力，杜絕字串 grep 假綠。
  - 最危險漂移（把『尚待生產驗證』軟化成『已完整驗證』）必須翻紅。

本檔為 additive，不改動任何既有守護測試或護欄本體。
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


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


# ---------------------------------------------------------------------------
# AC#5.1 — 半閉環聲明（真實 tag-push 端到端尚待生產驗證）
# ---------------------------------------------------------------------------

HALF_CLOSED_KEYWORDS = ("真實", "tag-push", "端到端", "生產驗證")


def check_half_closed(text: str) -> list[str]:
    """半閉環聲明必含關鍵詞集合＋『半閉環／尚待』修飾詞。回傳空 list = 齊備。"""
    problems: list[str] = []
    for kw in HALF_CLOSED_KEYWORDS:
        if kw not in text:
            problems.append(f"半閉環聲明缺關鍵詞：{kw!r}")
    if "半閉環" not in text and "尚待" not in text and "尚未" not in text:
        problems.append("半閉環聲明缺『半閉環／尚待／尚未』修飾詞（恐誤判為已 E2E）")
    return problems


def test_handoff_has_half_closed_disclaimer(handoff_text):
    problems = check_half_closed(handoff_text)
    assert problems == [], "AC#5.1：半閉環聲明缺漏：\n  - " + "\n  - ".join(problems)


def test_mutation_soften_to_fully_verified_turns_red():
    """最危險漂移：把『尚待生產驗證』改成『已完整驗證』→ 守護必翻紅。"""
    text = HANDOFF_MD.read_text(encoding="utf-8")
    assert "尚待" in text, "baseline 失效：原本就無『尚待』"
    mutated = text.replace("尚待", "已完整").replace("半閉環", "已完整閉環")
    assert mutated != text, "mutation 無效：未軟化半閉環字串"
    problems = check_half_closed(mutated)
    assert any("半閉環" in p or "尚待" in p for p in problems), (
        f"假綠：漂移為『已完整』後守護未翻紅，problems={problems}"
    )


# ---------------------------------------------------------------------------
# AC#5.2 — 發佈後具名人工核對步驟（本輪閉環關鍵）
# ---------------------------------------------------------------------------


def check_post_release_runbook(text: str) -> list[str]:
    """發佈後步驟必含：(a) 發佈『後』在 GitHub release 頁核對 (b) body 頂部即 Breaking
    (c) 四要素 (d) 逃生艙 TI_REQUIRE_CHOWN=warn/off (e) release: published 觸發 smoke。"""
    problems: list[str] = []
    norm = _norm(text)

    if "gh release view" not in norm and "release 頁" not in text:
        problems.append("缺『發佈後在 GitHub release 頁／gh release view 核對』步驟")
    # body 頂部即 Breaking 置頂
    if "## ⚠️ breaking changes" not in norm:
        problems.append("缺『body 頂部即 ## ⚠️ Breaking Changes』核對點")
    if "置頂" not in text and "頂部" not in text and "最上方" not in text:
        problems.append("缺『Breaking 區塊置頂／頂部』字樣")
    # 四要素
    if not ("四要素" in text or ("①" in text and "④" in text)):
        problems.append("缺四要素（①..④／『四要素』）核對點")
    # 逃生艙
    if "TI_REQUIRE_CHOWN=warn/off" not in text:
        problems.append("缺逃生艙字串 `TI_REQUIRE_CHOWN=warn/off`")
    # release: published 觸發 smoke
    if "release: published" not in text and "release：published" not in text:
        problems.append("缺『release: published 觸發 release-smoke』核對點")
    return problems


def test_handoff_has_post_release_runbook(handoff_text):
    problems = check_post_release_runbook(handoff_text)
    assert problems == [], "AC#5.2：發佈後人工步驟缺漏：\n  - " + "\n  - ".join(problems)


def test_mutation_drop_escape_hatch_turns_red():
    """反向 mutation：拿掉逃生艙字串 → 守護必翻紅。"""
    text = HANDOFF_MD.read_text(encoding="utf-8")
    assert "TI_REQUIRE_CHOWN=warn/off" in text, "baseline 失效：原本就無逃生艙字串"
    mutated = text.replace("TI_REQUIRE_CHOWN=warn/off", "TI_REQUIRE_CHOWN")
    assert mutated != text, "mutation 無效：未替換到逃生艙字串"
    problems = check_post_release_runbook(mutated)
    assert any("逃生艙" in p for p in problems), (
        f"假綠：拿掉逃生艙後守護未翻紅，problems={problems}"
    )


def test_mutation_drop_breaking_top_check_turns_red():
    """反向 mutation：拿掉『Breaking 置頂』核對點 → 守護必翻紅。"""
    text = HANDOFF_MD.read_text(encoding="utf-8")
    mutated = text.replace("置頂", "somewhere").replace("頂部", "somewhere").replace(
        "最上方", "somewhere"
    )
    assert mutated != text, "mutation 無效：未替換到置頂字樣"
    problems = check_post_release_runbook(mutated)
    assert any("置頂" in p or "頂部" in p for p in problems), (
        f"假綠：拿掉置頂核對點後守護未翻紅，problems={problems}"
    )


# ---------------------------------------------------------------------------
# AC#5.3 — 已閉環邊界勾稽既有守護測試
# ---------------------------------------------------------------------------


def test_handoff_cross_references_pretag_guard(handoff_text):
    """移交文件須勾稽 pretag 守護測試檔，讓『已閉環』邊界可被稽核。"""
    assert "test_qa_task4_pretag_breaking_outlets.py" in handoff_text, (
        "AC#5.3：未勾稽 pretag 守護測試檔，無法界定已閉環邊界"
    )


# 文件以「函式名」而非「行號」勾稽（行號會隨被引檔增刪漂移且無護欄）。
# 這裡把每個被引函式名綁到其所在檔，並實際驗證該符號仍存在——引用一漂移即翻紅。
_REFERENCED_SYMBOLS = {
    "tests/autopilot/test_qa_task4_pretag_breaking_outlets.py": (
        "test_pretag_outlet_carries_block",
        "test_pretag_effective_version_matches_pyproject_in_both_outlets",
        "test_black_sample_missing_block_pairs_red",
        "test_black_sample_missing_each_element_pairs_red",
        "test_black_sample_stale_effective_version_pairs_red",
    ),
    "tests/autopilot/_release_check.py": ("version_matches_effective",),
    "tests/autopilot/test_qa_task4_release_docs_dod.py": (
        "test_task4_commit_does_not_alter_release_smoke_trigger",
    ),
}


@pytest.mark.parametrize("rel_path, symbols", _REFERENCED_SYMBOLS.items())
def test_handoff_symbol_references_are_live(handoff_text, rel_path, symbols):
    """文件引用的函式名必須：(1) 真的出現在文件內 (2) 真的存在於被引檔案。

    這是把行號式漂移源換成有護欄的函式名引用後的守衛——任一被引函式改名／刪除，
    或文件與被引檔失聯，此測試即翻紅，杜絕文件靜默爛掉。
    """
    src = (ROOT / rel_path).read_text(encoding="utf-8")
    for sym in symbols:
        assert sym in handoff_text, f"移交文件未引用函式名 `{sym}`（已閉環邊界失聯）"
        assert f"def {sym}" in src, (
            f"引用漂移：`{sym}` 已不存在於 {rel_path}（函式改名／刪除）"
        )
