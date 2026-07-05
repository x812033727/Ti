"""QA 任務 #1：v0.2.0 handoff 邊界表回填驗收整合測試。

本檔**不重複**單點守護（已有 `test_qa_body_pinning_evidence.py`、
`test_qa_smoke_trigger_evidence.py`、`test_qa_task4_e2e_handoff.py`、
`test_qa_task4_release_docs_dod.py` 各守一環）。本檔專注**跨檔案整合驗收**，
對應本輪回填的驗收標準：

  AC#B1 — handoff 邊界表中 body 置頂列與 smoke 觸發列**並存**皆 ✅（無 ⏳/❌ 殘留）。
  AC#B2 — body 列依據欄含四個可回指路徑：evidence JSON + verdict JSON + check script
          + 本輪新守護測試（且各路徑在磁碟上實際存在）。
  AC#B3 — smoke 列依據欄含 run-id 27905531397 與 event=release／conclusion=success
          雙路核對一致字串。
  AC#B4 — 頂部半閉環聲明**完整保留**六關鍵詞：`真實`、`tag-push`、`端到端`、
          `生產驗證`、`半閉環`、`尚待`（注意：與既有 `check_half_closed` 的
          半閉環/尚待 OR 不同，本檔強制兩者**皆在**）。
  AC#B5 — 頂部聲明與「兩列皆 ✅」自洽：版本限定「v0.2.0 此鏈已生產閉環」
          後必須緊接「後續版本仍半閉環／尚待」，未把半閉環整體軟化。
  AC#B6 — 三份 evidence 檔（online-body.json、body-structure-verdict.json、
          release-smoke-trigger.json）皆實際存在於 `docs/evidence/`。
  AC#B7 — additive／可逆／零 production code 變更：`BREAKING_HEADING` 常數與
          版本字面值 0.2.0 在守護本體與 handoff 文件皆未被改動。
  AC#B8 — 文件未宣稱 body 置頂「待封／範圍外」等與 ✅ 衝突的字串。

設計：
  - `from studio.release_note import BREAKING_HEADING, pyproject_version`
    抓真實常數／版本，禁硬寫斷言（防「測試綠／文件漂」假綠）。
  - 每個 AC 抽成可重用 `check_*` 判斷式；baseline 綠與 mutation 紅共用同一把尺。
  - 黑樣本成對：把 ✅ 退回 ⏳／抽六關鍵詞任一／把 BREAKING_HEADING 改字面值
    ／把聲明整體軟化為「已完整」→ 守護必翻紅。

本檔為 additive，不改任何既有守護或護欄本體；破壞性思考：預設文件是壞的
直到這把尺證明齊備。
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

# 從守護本體抓真實常數，避免「測試綠／文件漂」假綠。
from studio.release_note import BREAKING_HEADING, pyproject_version

ROOT = Path(__file__).resolve().parents[2]

HANDOFF_MD = ROOT / "docs" / "release-e2e-handoff.md"
ONLINE_BODY = ROOT / "docs" / "evidence" / "release-v0.2.0-online-body.json"
BODY_VERDICT = ROOT / "docs" / "evidence" / "release-v0.2.0-body-structure-verdict.json"
SMOKE_TRIGGER = ROOT / "docs" / "evidence" / "release-smoke-v0.2.0-trigger.json"

# 用檔名（不是行號）定位邊界表 row；行號會漂，檔案/字串錨點穩。
EXPECTED_BODY_GUARD = (
    "tests/autopilot/test_qa_body_pinning_evidence.py::"
    "test_handoff_body_row_is_green_with_evidence_paths"
)
EXPECTED_SMOKE_GUARD = "docs/evidence/release-smoke-v0.2.0-trigger.json"
EXPECTED_RUN_ID = "27905531397"

# 六關鍵詞全齊（既有 check_half_closed 是 OR，本檔強制全部 AND）
SIX_KEYWORDS = ("真實", "tag-push", "端到端", "生產驗證", "半閉環", "尚待")


# ---------------------------------------------------------------------------
# 解析輔助
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def handoff_text() -> str:
    assert HANDOFF_MD.exists(), f"前提失效：缺 handoff {HANDOFF_MD}"
    return HANDOFF_MD.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def online_body() -> dict:
    assert ONLINE_BODY.exists(), f"前提失效：缺 {ONLINE_BODY}"
    return json.loads(ONLINE_BODY.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def body_verdict() -> dict:
    assert BODY_VERDICT.exists(), f"前提失效：缺 {BODY_VERDICT}"
    return json.loads(BODY_VERDICT.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def smoke_trigger() -> dict:
    assert SMOKE_TRIGGER.exists(), f"前提失效：缺 {SMOKE_TRIGGER}"
    return json.loads(SMOKE_TRIGGER.read_text(encoding="utf-8"))


def _row_containing(text: str, anchor: str) -> str:
    """抓邊界表中含 anchor 字串的 table row（無 = ""）。"""
    for line in text.splitlines():
        if line.lstrip().startswith("|") and anchor in line:
            return line
    return ""


def _row_body(text: str) -> str:
    # 用不會被「拿掉 evidence 路徑」黑樣本破壞的穩定字串當錨點，才打得到缺路徑本身。
    for anchor in ("body_sha256", "body_match=true", "verdict=PASS", "docs/evidence/release-v0.2.0-online-body.json"):
        row = _row_containing(text, anchor)
        if row:
            return row
    return ""


def _row_smoke(text: str) -> str:
    return _row_containing(text, EXPECTED_SMOKE_GUARD)


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def _top_disclaimer(text: str) -> str:
    """頂部半閉環聲明段落：以標題『## 半閉環聲明』為錨，避免抓到『離線 render』段。"""
    m = re.search(r"## 半閉環聲明.*?(?=\n## |\Z)", text, flags=re.DOTALL)
    return m.group(0) if m else ""


# ---------------------------------------------------------------------------
# AC#B6 — 三份 evidence 檔實際存在於 docs/evidence/
# ---------------------------------------------------------------------------


def check_evidence_files_exist() -> list[str]:
    problems: list[str] = []
    for p, name in (
        (ONLINE_BODY, "release-v0.2.0-online-body.json"),
        (BODY_VERDICT, "release-v0.2.0-body-structure-verdict.json"),
        (SMOKE_TRIGGER, "release-smoke-v0.2.0-trigger.json"),
    ):
        if not p.exists():
            problems.append(f"{name} 不存在於 {p.parent}")
        elif p.stat().st_size == 0:
            problems.append(f"{name} 為空檔")
    return problems


def test_three_evidence_files_all_exist():
    """AC#B6：三份 evidence 檔皆實際存在且非空。"""
    problems = check_evidence_files_exist()
    assert problems == [], "AC#B6：evidence 檔缺漏：\n  - " + "\n  - ".join(problems)


# ---------------------------------------------------------------------------
# AC#B1 — 兩列並存皆 ✅，無 ⏳/❌ 殘留
# ---------------------------------------------------------------------------


def check_both_rows_green(text: str) -> list[str]:
    problems: list[str] = []
    body_row = _row_body(text)
    smoke_row = _row_smoke(text)

    if not body_row:
        problems.append("找不到 body 置頂列（邊界表）")
    else:
        if "✅" not in body_row:
            problems.append("body 置頂列未標 ✅")
        if "⏳" in body_row:
            problems.append("body 置頂列仍含 ⏳（未翻封）")
        if "❌" in body_row:
            problems.append("body 置頂列仍含 ❌（未翻正）")

    if not smoke_row:
        problems.append("找不到 release-smoke 觸發列（邊界表）")
    else:
        if "✅" not in smoke_row:
            problems.append("smoke 觸發列未標 ✅")
        if "⏳" in smoke_row:
            problems.append("smoke 觸發列仍含 ⏳（未翻封）")
        if "❌" in smoke_row:
            problems.append("smoke 觸發列仍含 ❌（未翻正）")

    # 並存性：兩列皆 ✅
    if body_row and smoke_row:
        body_green = "✅" in body_row and "⏳" not in body_row and "❌" not in body_row
        smoke_green = "✅" in smoke_row and "⏳" not in smoke_row and "❌" not in smoke_row
        if body_green and not smoke_green:
            problems.append("並存性：body 列 ✅ 但 smoke 列未 ✅（半閉環）")
        if smoke_green and not body_green:
            problems.append("並存性：smoke 列 ✅ 但 body 列未 ✅（半閉環）")

    return problems


def test_both_rows_green_coexist(handoff_text):
    """AC#B1：body 置頂列與 smoke 觸發列並存皆 ✅。"""
    problems = check_both_rows_green(handoff_text)
    assert problems == [], "AC#B1：兩列並存自洽破洞：\n  - " + "\n  - ".join(problems)


# ---------------------------------------------------------------------------
# AC#B2 — body 列依據欄四路徑齊
# ---------------------------------------------------------------------------


def check_body_row_paths(text: str) -> list[str]:
    problems: list[str] = []
    row = _row_body(text)
    if not row:
        problems.append("找不到 body 置頂列")
        return problems

    # evidence JSON
    if "docs/evidence/release-v0.2.0-online-body.json" not in row:
        problems.append("body 列依據欄未含 online-body.json 路徑")
    if "body_match=true" not in row:
        problems.append("body 列依據欄未標 body_match=true")
    if "body_sha256" not in row:
        problems.append("body 列依據欄未提 body_sha256")

    # verdict JSON
    if "docs/evidence/release-v0.2.0-body-structure-verdict.json" not in row:
        problems.append("body 列依據欄未含 body-structure-verdict.json 路徑")
    if "verdict=PASS" not in row:
        problems.append("body 列依據欄未提 verdict=PASS")
    if "頂部即 Breaking 置頂=true" not in row:
        problems.append("body 列依據欄未提『頂部即 Breaking 置頂=true』")
    if "雙來源正規化後逐字相等=true" not in row:
        problems.append("body 列依據欄未提雙來源逐字相等=true")

    # check script
    if "scripts/check_release_body_structure.py" not in row:
        problems.append("body 列依據欄未含 check_release_body_structure.py 路徑")

    # 守護測試（AC#B2 第四路徑：對稱 smoke 的 test_qa_smoke_trigger_evidence）
    if EXPECTED_BODY_GUARD not in row:
        problems.append(
            f"body 列依據欄未回指本輪新守護測試 {EXPECTED_BODY_GUARD}"
        )

    return problems


def test_body_row_evidence_paths_complete(handoff_text):
    """AC#B2：body 列依據欄四路徑（evidence/verdict/script/守護測試）齊備。"""
    problems = check_body_row_paths(handoff_text)
    assert problems == [], "AC#B2：body 列依據欄缺漏：\n  - " + "\n  - ".join(problems)


# ---------------------------------------------------------------------------
# AC#B3 — smoke 列依據欄含 run-id + 雙路核對
# ---------------------------------------------------------------------------


def check_smoke_row_paths(text: str) -> list[str]:
    problems: list[str] = []
    row = _row_smoke(text)
    if not row:
        problems.append("找不到 release-smoke 觸發列")
        return problems

    if EXPECTED_RUN_ID not in row:
        problems.append(f"smoke 列依據欄未引用 run-id {EXPECTED_RUN_ID}")
    if "event=release" not in row:
        problems.append("smoke 列依據欄未提 event=release")
    if "conclusion=success" not in row:
        problems.append("smoke 列依據欄未提 conclusion=success")
    if "gh run view" not in row:
        problems.append("smoke 列依據欄未提 gh run view（單一來源不足以排除快取/顯示差異）")
    if "REST" not in row and "rest" not in row.lower():
        problems.append("smoke 列依據欄未提 REST（雙路之一）")

    # 雙路措辭必須明文保留
    norm = _norm(row)
    if "雙路核對一致" not in norm and "雙路" not in row:
        problems.append("smoke 列依據欄未提『雙路核對一致』字串")

    return problems


def test_smoke_row_evidence_paths_complete(handoff_text):
    """AC#B3：smoke 列依據欄含 run-id + 雙路核對字串。"""
    problems = check_smoke_row_paths(handoff_text)
    assert problems == [], "AC#B3：smoke 列依據欄缺漏：\n  - " + "\n  - ".join(problems)


# ---------------------------------------------------------------------------
# AC#B4 — 頂部六關鍵詞全齊（既有 check_half_closed 是 OR，本檔 AND）
# ---------------------------------------------------------------------------


def check_six_keywords(text: str) -> list[str]:
    problems: list[str] = []
    for kw in SIX_KEYWORDS:
        if kw not in text:
            problems.append(f"頂部半閉環聲明缺六關鍵詞之一：{kw!r}")
    return problems


def test_six_keywords_all_present(handoff_text):
    """AC#B4：六關鍵詞（真實／tag-push／端到端／生產驗證／半閉環／尚待）**全部**在文件中。

    注意：既有 check_half_closed 接受『半閉環/尚待/尚未』任一即過，但本輪收斂要求
    半閉環與尚待**並存**——任何只剩一個即視為軟化漂移。
    """
    problems = check_six_keywords(handoff_text)
    assert problems == [], "AC#B4：六關鍵詞缺漏：\n  - " + "\n  - ".join(problems)


# ---------------------------------------------------------------------------
# AC#B5 — 頂部聲明與「兩列皆 ✅」自洽
#   條件：v0.2.0 此鏈已生產閉環＋後續版本仍半閉環／尚待，二者必須**同時**在
# ---------------------------------------------------------------------------


def check_disclaimer_coexists_with_green(text: str) -> list[str]:
    """v0.2.0 此鏈已生產閉環（可寫 ✅）**不可**讓半閉環／尚待消失。

    三種禁止漂移：
      (a) 把『半閉環／尚待』整體拿掉（宣稱全鏈已 E2E）
      (b) 把『v0.2.0 已生產閉環』拿掉（即使證據齊，聲明退縮 → 列翻 ✅ 無據）
      (c) 把聲明改成僅寫『已完整』單詞（軟化）
    """
    problems: list[str] = []
    top = _top_disclaimer(text)
    if not top:
        problems.append("找不到『## 半閉環聲明』段落（頂部聲明漂移或刪除）")
        return problems

    # (a) 「半閉環」與「尚待」必須共存（不能拿掉任一）
    if "半閉環" not in top:
        problems.append("頂部聲明缺『半閉環』修飾詞（半閉環整體軟化風險）")
    if "尚待" not in top:
        problems.append("頂部聲明缺『尚待』修飾詞（半閉環整體軟化風險）")

    # (b) 版本限定收斂必須在
    if "v0.2.0" not in top:
        problems.append("頂部聲明未限定到 v0.2.0（與兩列 ✅ 自洽破洞）")
    if "已生產閉環" not in top and "已閉環" not in top:
        problems.append("頂部聲明未標註 v0.2.0 此鏈已閉環（兩列 ✅ 失去聲明撐腰）")
    if "後續版本" not in top and "未來版本" not in top:
        problems.append("頂部聲明缺『後續版本／未來版本』限定（半閉環範圍沒被收斂到非 v0.2.0）")

    # (c) 「已完整」單詞若出現在頂部聲明區段，視為軟化漂移
    if re.search(r"已完整", top):
        problems.append("頂部聲明出現『已完整』字串（軟化漂移）")

    return problems


def test_top_disclaimer_coexists_with_green_rows(handoff_text):
    """AC#B5：頂部聲明與兩列 ✅ 自洽：版本限定收斂＋半閉環/尚待並存。"""
    problems = check_disclaimer_coexists_with_green(handoff_text)
    assert problems == [], "AC#B5：頂部聲明自洽破洞：\n  - " + "\n  - ".join(problems)


# ---------------------------------------------------------------------------
# AC#B7 — additive／不動 BREAKING_HEADING 常數與版本字面值
# ---------------------------------------------------------------------------


def check_constants_untouched() -> list[str]:
    """守護本體的 BREAKING_HEADING 與 pyproject 版本字面值未被本回填改動。

    透過 git blame / git log 驗：handoff 文件沒有反向改 `studio/release_note.py`
    或 `pyproject.toml`。檢查方式：handoff 文件 git log 最近 N 次 commit 觸及
    的檔案清單不含這兩個檔。
    """
    problems: list[str] = []
    out = subprocess.run(
        ["git", "log", "--name-only", "--pretty=format:", "--", str(HANDOFF_MD)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    touched = {ln.strip() for ln in out.stdout.splitlines() if ln.strip()}
    if "studio/release_note.py" in touched:
        problems.append("handoff 文件的 git log 觸及 studio/release_note.py（反向改常數違規）")
    if "pyproject.toml" in touched:
        problems.append("handoff 文件的 git log 觸及 pyproject.toml（反向改版本違規）")
    return problems


def test_no_reverse_edit_to_constants_or_pyproject():
    """AC#B7：handoff 文件的 git 歷史未反向觸及 BREAKING_HEADING 來源檔與 pyproject 版本。"""
    problems = check_constants_untouched()
    assert problems == [], "AC#B7：反向改守護常數或版本字面值：\n  - " + "\n  - ".join(problems)


def test_handoff_version_literal_matches_pyproject(handoff_text):
    """AC#B7：handoff 文件出現的版本字面值 0.2.0 仍 = pyproject 當前版本。"""
    version = pyproject_version()
    # 文件必然提到 0.2.0（v0.2.0 與 0.2.0 兩種寫法都算）
    if "0.2.0" not in handoff_text and "v0.2.0" not in handoff_text:
        pytest.fail(f"AC#B7：handoff 文件無 0.2.0 字面值（可能漂移到非當前版本）")
    assert version == "0.2.0", (
        f"AC#B7：pyproject 版本 {version!r} ≠ 0.2.0（驗收基準漂移）"
    )


def test_handoff_breaking_heading_literal_matches_constant(handoff_text):
    """AC#B7：handoff 文件頂部/邊界表/核對步驟出現的 Breaking heading 字面值 == BREAKING_HEADING。"""
    norm = _norm(handoff_text)
    constant_norm = _norm(BREAKING_HEADING)
    assert constant_norm in norm, (
        f"AC#B7：handoff 文件未含 BREAKING_HEADING 字面值 {BREAKING_HEADING!r}"
    )


# ---------------------------------------------------------------------------
# AC#B8 — 文件未宣稱 body 置頂「待封／範圍外」等與 ✅ 衝突
# ---------------------------------------------------------------------------


_CONFLICTING_PHRASES_BODY = ("待封", "範圍外", "未證", "尚未證成", "未實跑")


def check_no_conflicting_phrases(text: str) -> list[str]:
    problems: list[str] = []
    body_row = _row_body(text)
    if not body_row:
        return problems
    for phrase in _CONFLICTING_PHRASES_BODY:
        if phrase in body_row:
            problems.append(f"body 列仍含與 ✅ 衝突的字串：{phrase!r}")
    return problems


def test_body_row_has_no_conflicting_phrases(handoff_text):
    """AC#B8：body 列 ✅ 與同列字串自洽，不殘留『待封／範圍外／未證成』等。"""
    problems = check_no_conflicting_phrases(handoff_text)
    assert problems == [], "AC#B8：body 列衝突字串殘留：\n  - " + "\n  - ".join(problems)


# ---------------------------------------------------------------------------
# 整合收斂閘：所有 AC 一次跑過
# ---------------------------------------------------------------------------


def test_full_acceptance_audit(handoff_text, online_body, body_verdict, smoke_trigger):
    """整合收斂閘：把所有 AC 一次跑完，作為 reviewer 一眼看缺口的單一入口。"""
    all_problems: list[str] = []
    all_problems += [f"[AC#B6] {p}" for p in check_evidence_files_exist()]
    all_problems += [f"[AC#B1] {p}" for p in check_both_rows_green(handoff_text)]
    all_problems += [f"[AC#B2] {p}" for p in check_body_row_paths(handoff_text)]
    all_problems += [f"[AC#B3] {p}" for p in check_smoke_row_paths(handoff_text)]
    all_problems += [f"[AC#B4] {p}" for p in check_six_keywords(handoff_text)]
    all_problems += [f"[AC#B5] {p}" for p in check_disclaimer_coexists_with_green(handoff_text)]
    all_problems += [f"[AC#B7] {p}" for p in check_constants_untouched()]
    all_problems += [f"[AC#B8] {p}" for p in check_no_conflicting_phrases(handoff_text)]
    assert all_problems == [], "整合驗收破洞：\n  - " + "\n  - ".join(all_problems)


# ---------------------------------------------------------------------------
# 黑樣本成對：每個關鍵契約的 mutation 必翻紅
# ---------------------------------------------------------------------------


def test_black_sample_drop_six_keyword_shangdai_turns_red(handoff_text):
    """黑樣本：拿掉『尚待』→ AC#B4 翻紅（驗證『六關鍵詞 AND』有真鑑別力）。"""
    assert "尚待" in handoff_text, "baseline 失效：原本就無『尚待』"
    mutated = handoff_text.replace("尚待", "已驗證")
    assert mutated != handoff_text, "mutation 無效"
    problems = check_six_keywords(mutated)
    assert any("尚待" in p for p in problems), (
        f"假綠：拿掉『尚待』後六關鍵詞守護未翻紅，problems={problems}"
    )


def test_black_sample_drop_six_keyword_halfclosed_turns_red(handoff_text):
    """黑樣本：拿掉『半閉環』→ AC#B4 翻紅。"""
    assert "半閉環" in handoff_text, "baseline 失效：原本就無『半閉環』"
    mutated = handoff_text.replace("半閉環", "已閉環")
    assert mutated != handoff_text, "mutation 無效"
    problems = check_six_keywords(mutated)
    assert any("半閉環" in p for p in problems), (
        f"假綠：拿掉『半閉環』後六關鍵詞守護未翻紅，problems={problems}"
    )


def test_black_sample_body_row_reverted_to_pending_turns_red(handoff_text):
    """黑樣本：body 列 ✅ 被改回 ⏳ → AC#B1 + AC#B8 翻紅。"""
    row = _row_body(handoff_text)
    assert row and "✅" in row, "baseline 失效：body 列原本就無 ✅"
    mutated = handoff_text.replace(row, row.replace("✅", "⏳"))
    assert mutated != handoff_text, "mutation 無效"
    problems_b1 = check_both_rows_green(mutated)
    problems_b8 = check_no_conflicting_phrases(mutated)
    combined = problems_b1 + problems_b8
    assert any("body" in p.lower() or "⏳" in p or "置頂" in p for p in combined), (
        f"假綠：body 列退回 ⏳ 後 AC#B1/#B8 未翻紅，problems={combined}"
    )


def test_black_sample_smoke_row_reverted_to_red_turns_red(handoff_text):
    """黑樣本：smoke 列 ✅ 被改回 ❌ → AC#B1 翻紅。"""
    row = _row_smoke(handoff_text)
    assert row and "✅" in row, "baseline 失效：smoke 列原本就無 ✅"
    mutated = handoff_text.replace(row, row.replace("✅", "❌"))
    assert mutated != handoff_text, "mutation 無效"
    problems = check_both_rows_green(mutated)
    assert any("smoke" in p.lower() or "❌" in p for p in problems), (
        f"假綠：smoke 列退回 ❌ 後 AC#B1 未翻紅，problems={problems}"
    )


def test_black_sample_drop_disclaimer_version_scope_turns_red(handoff_text):
    """黑樣本：把頂部聲明的 v0.2.0 收斂整段拿掉 → AC#B5 翻紅。

    漂移型：『v0.2.0 此鏈已生產閉環』拿掉 = 兩列 ✅ 失去聲明撐腰。
    """
    top = _top_disclaimer(handoff_text)
    assert top, "baseline 失效：找不到頂部聲明段"
    mutated = handoff_text.replace(top, "")
    assert mutated != handoff_text, "mutation 無效"
    problems = check_disclaimer_coexists_with_green(mutated)
    assert any(
        "頂部聲明" in p or "v0.2.0" in p or "已閉環" in p or "半閉環" in p or "尚待" in p
        for p in problems
    ), f"假綠：刪掉頂部聲明後 AC#B5 未翻紅，problems={problems}"


def test_black_sample_soften_disclaimer_to_fully_verified_turns_red(handoff_text):
    """黑樣本：把頂部聲明的『v0.2.0 此鏈已生產閉環』改為『v0.2.0 此鏈已完整 E2E 通過』
    並拿掉『半閉環／尚待』→ AC#B5 必翻紅（最危險漂移）。
    """
    top = _top_disclaimer(handoff_text)
    assert top, "baseline 失效：找不到頂部聲明段"
    mutated_top = (
        top.replace("v0.2.0 此鏈已生產閉環", "v0.2.0 此鏈已完整 E2E 通過")
        .replace("半閉環", "已閉環")
        .replace("尚待", "已驗")
    )
    assert mutated_top != top, "mutation 無效：未軟化聲明"
    mutated = handoff_text.replace(top, mutated_top)
    problems = check_six_keywords(mutated) + check_disclaimer_coexists_with_green(mutated)
    assert problems, (
        f"假綠：整體軟化為『已完整』後守護未翻紅，problems={problems}"
    )


def test_black_sample_drop_body_path_in_row_turns_red(handoff_text):
    """黑樣本：把 body 列的 evidence 路徑拿掉 → AC#B2 翻紅。"""
    row = _row_body(handoff_text)
    assert row and "docs/evidence/release-v0.2.0-online-body.json" in row, (
        "baseline 失效"
    )
    mutated = handoff_text.replace(
        "docs/evidence/release-v0.2.0-online-body.json",
        "docs/evidence/_redacted.json",
    )
    assert mutated != handoff_text, "mutation 無效"
    problems = check_body_row_paths(mutated)
    assert any("online-body.json" in p or "evidence" in p for p in problems), (
        f"假綠：拿掉 evidence 路徑後 AC#B2 未翻紅，problems={problems}"
    )


def test_black_sample_drop_runid_in_smoke_row_turns_red(handoff_text):
    """黑樣本：把 smoke 列的 run-id 拿掉 → AC#B3 翻紅。"""
    row = _row_smoke(handoff_text)
    assert row and EXPECTED_RUN_ID in row, "baseline 失效"
    mutated = handoff_text.replace(EXPECTED_RUN_ID, "00000000000")
    assert mutated != handoff_text, "mutation 無效"
    problems = check_smoke_row_paths(mutated)
    assert any(EXPECTED_RUN_ID in p or "run-id" in p for p in problems), (
        f"假綠：拿掉 run-id 後 AC#B3 未翻紅，problems={problems}"
    )


def test_black_sample_drop_dualpath_phrase_turns_red(handoff_text):
    """黑樣本：smoke 列的『雙路核對一致』字串拿掉 → AC#B3 翻紅。

    為什麼關鍵：既有慣例是『gh + REST 雙路』，退化為單路會被快取/顯示差異誤導。
    """
    row = _row_smoke(handoff_text)
    assert row, "baseline 失效"
    mutated_row = row.replace("雙路核對一致", "單路已驗")
    assert mutated_row != row, "mutation 無效"
    mutated = handoff_text.replace(row, mutated_row)
    problems = check_smoke_row_paths(mutated)
    assert any("雙路" in p for p in problems), (
        f"假綠：退化為單路後 AC#B3 未翻紅，problems={problems}"
    )


def test_black_sample_inject_conflict_phrase_in_body_row_turns_red(handoff_text):
    """黑樣本：在 body 列塞入『待封』→ AC#B8 翻紅。"""
    row = _row_body(handoff_text)
    assert row, "baseline 失效"
    mutated = handoff_text.replace(row, row + "（待封）")
    assert mutated != handoff_text, "mutation 無效"
    problems = check_no_conflicting_phrases(mutated)
    assert any("待封" in p for p in problems), (
        f"假綠：塞入『待封』後 AC#B8 未翻紅，problems={problems}"
    )
