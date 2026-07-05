"""QA 任務 #3：release-smoke 觸發證據守門測試。

守護兩份互相勾稽的產出，對應任務 #2/#1 的封口：
  - `docs/evidence/release-smoke-v0.2.0-trigger.json`（smoke 觸發證據 SSOT）
  - `docs/release-e2e-handoff.md`（邊界表：smoke 觸發列須為 ✅ 並引用 run-id）

核心不變式（把誠實性編成測試約束，不靠人自律）：
  1. 證據須為實跑雙路核對：`gh run view` 與 REST 兩路關鍵欄位相等（記於 `cross_checks`）。
  2. 只有 `verification_status == "verified"` 且 `event == "release"`／`conclusion == "success"`
     時，handoff 的 smoke 觸發列才可為 ✅——此「gate 蘊含」由 `check_gate` 強制。
  3. 證據須同時記 success run 與被取代的 failure run，不得只報喜藏憂。

設計沿用團隊硬規則：每條契約抽成 `check_*` 判斷式，baseline 綠與 mutation 紅共用同一把尺，
證明守護有真鑑別力；成對黑樣本（竄改 run-id／✅ 翻回 ❌／抽掉 event 欄／抽掉 failure run／
verification 退回 pending 但 ✅ 未退）任一都必須翻紅。本檔 additive、不打網路、不重跑 gh。
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
EVIDENCE_JSON = ROOT / "docs" / "evidence" / "release-smoke-v0.2.0-trigger.json"
HANDOFF_MD = ROOT / "docs" / "release-e2e-handoff.md"

EXPECTED_RUN_ID = "27905531397"


@pytest.fixture(scope="module")
def evidence() -> dict:
    assert EVIDENCE_JSON.exists(), f"前提失效：缺證據檔 {EVIDENCE_JSON}"
    return json.loads(EVIDENCE_JSON.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def handoff_text() -> str:
    assert HANDOFF_MD.exists(), f"前提失效：缺移交文件 {HANDOFF_MD}"
    return HANDOFF_MD.read_text(encoding="utf-8")


def _smoke_row(text: str) -> str:
    """抽出 handoff 邊界表中『release-smoke 觸發』那一列（table row）。

    以 `release-smoke.yml` 錨定——body 置頂列不含此字串，避免抓錯列。
    """
    for line in text.splitlines():
        if line.lstrip().startswith("|") and "`release-smoke.yml`" in line:
            return line
    return ""


# ---------------------------------------------------------------------------
# 契約一：證據 JSON 自身完整且雙路一致
# ---------------------------------------------------------------------------


def check_evidence(obj: dict) -> list[str]:
    problems: list[str] = []

    # 關鍵欄位齊備且值正確
    if obj.get("verification_status") != "verified":
        problems.append("verification_status 非 verified（未完成實跑核對不得宣稱閉環）")
    if obj.get("run_id") != EXPECTED_RUN_ID:
        problems.append(f"run_id 不為 {EXPECTED_RUN_ID}")
    if obj.get("event") != "release":
        problems.append("缺/錯 event 欄（須為 release，證明由 release: published 觸發）")
    if obj.get("status") != "completed":
        problems.append("status 非 completed")
    if obj.get("conclusion") != "success":
        problems.append("conclusion 非 success")

    # 雙路可重跑命令字串須在（證據可重驗、非手抄）
    if not obj.get("gh_run_view_command"):
        problems.append("缺 gh_run_view_command")
    if not obj.get("rest_get_command"):
        problems.append("缺 rest_get_command")

    # 雙路快照關鍵欄位須相等
    gh = obj.get("gh_run_view") or {}
    rest = obj.get("rest_run") or {}
    run_ids = {str(gh.get("databaseId")), str(rest.get("id")), str(obj.get("run_id"))}
    if run_ids != {EXPECTED_RUN_ID}:
        problems.append("gh_run_view.databaseId／rest_run.id／run_id 未三者一致於 run_id")
    for gk, rk in (("event", "event"), ("status", "status"), ("conclusion", "conclusion")):
        if gh.get(gk) != rest.get(rk):
            problems.append(f"雙路 {gk} 不一致：gh={gh.get(gk)!r} rest={rest.get(rk)!r}")

    # cross_checks 全綠
    checks = obj.get("cross_checks") or {}
    if not checks:
        problems.append("缺 cross_checks")
    for k, v in checks.items():
        if v is not True:
            problems.append(f"cross_checks.{k} 非 True")

    # 只報喜藏憂＝不誠實：須同時記被取代的 failure run
    fail = obj.get("superseded_failure_run") or {}
    if not fail:
        problems.append("缺 superseded_failure_run（不得只報 success 藏 failure）")
    else:
        if fail.get("event") != "release":
            problems.append("superseded_failure_run.event 非 release")
        if fail.get("conclusion") != "failure":
            problems.append("superseded_failure_run.conclusion 非 failure")
        if fail.get("run_id") == EXPECTED_RUN_ID:
            problems.append("superseded_failure_run 與 success run 為同一 run（未如實區分）")
    return problems


def test_evidence_is_verified_dualpath_consistent(evidence):
    problems = check_evidence(evidence)
    assert problems == [], "證據契約缺漏：\n  - " + "\n  - ".join(problems)


# ---------------------------------------------------------------------------
# 契約二：handoff smoke 觸發列為 ✅ 且引用 run-id
# ---------------------------------------------------------------------------


def check_handoff_smoke_row(text: str) -> list[str]:
    problems: list[str] = []
    row = _smoke_row(text)
    if not row:
        problems.append("找不到 handoff 的 release-smoke 觸發列")
        return problems
    if "✅" not in row:
        problems.append("smoke 觸發列未標 ✅")
    if "❌" in row:
        problems.append("smoke 觸發列仍含 ❌（未翻正）")
    if EXPECTED_RUN_ID not in row:
        problems.append(f"smoke 觸發列未引用 run-id {EXPECTED_RUN_ID}")
    return problems


def test_handoff_smoke_row_is_green_with_runid(handoff_text):
    problems = check_handoff_smoke_row(handoff_text)
    assert problems == [], "handoff smoke 列缺漏：\n  - " + "\n  - ".join(problems)


# ---------------------------------------------------------------------------
# 契約三：gate 蘊含——handoff ✅ 必須有 verified 證據撐腰（誠實性約束）
# ---------------------------------------------------------------------------


def check_gate(obj: dict, text: str) -> list[str]:
    """handoff smoke 列為 ✅ ⟺ 證據 verified 且 event=release 且 conclusion=success。

    任一方偏移（✅ 卻無實證，或有實證卻未翻 ✅）都翻紅——擋死假綠與遺漏。
    """
    problems: list[str] = []
    row = _smoke_row(text)
    handoff_green = ("✅" in row) and (EXPECTED_RUN_ID in row) and ("❌" not in row)
    evidence_ok = (
        obj.get("verification_status") == "verified"
        and obj.get("event") == "release"
        and obj.get("conclusion") == "success"
        and obj.get("run_id") == EXPECTED_RUN_ID
    )
    if handoff_green and not evidence_ok:
        problems.append("handoff 標 ✅ 但證據未達 verified/release/success（假綠）")
    if evidence_ok and not handoff_green:
        problems.append("證據已 verified 但 handoff 未翻 ✅（遺漏封口）")
    # run-id 一致性：handoff 引用值須與證據一致
    if EXPECTED_RUN_ID in row and obj.get("run_id") != EXPECTED_RUN_ID:
        problems.append("handoff run-id 與證據 run_id 不一致")
    return problems


def test_gate_handoff_green_iff_evidence_verified(evidence, handoff_text):
    problems = check_gate(evidence, handoff_text)
    assert problems == [], "gate 契約破裂：\n  - " + "\n  - ".join(problems)


# ---------------------------------------------------------------------------
# 成對黑樣本：任一 mutation 都必須翻紅（自證判別力）
# ---------------------------------------------------------------------------


def test_black_sample_tampered_runid_turns_red(evidence, handoff_text):
    """竄改證據 run-id → 與 handoff 引用值失聯，gate 翻紅。

    竄改值須與 baseline 現值明確不同，否則「竄改態＝提交態」黑樣本會喪失判別力
    （跨場次教訓：run-id 字串在 ≠ 判別力）。此處以斷言強制 mutation 真的改動了值。
    """
    tampered = "deadbeef-not-a-run-id"
    assert tampered != EXPECTED_RUN_ID, "黑樣本竄改值不得與 EXPECTED_RUN_ID 相同"
    assert tampered != evidence.get("run_id"), (
        "黑樣本竄改值與證據 baseline run_id 撞值，喪失判別力"
    )
    mutated = copy.deepcopy(evidence)
    mutated["run_id"] = tampered
    problems = check_gate(mutated, handoff_text) + check_evidence(mutated)
    assert problems, "假綠：竄改 run-id 後守護未翻紅"


def test_black_sample_green_reverted_to_red_turns_red(evidence, handoff_text):
    """handoff ✅ 被改回 ❌ → smoke 列契約與 gate 皆翻紅。"""
    row = _smoke_row(handoff_text)
    assert "✅" in row, "baseline 失效：smoke 列原本就無 ✅"
    mutated_text = handoff_text.replace(row, row.replace("✅", "❌"))
    assert mutated_text != handoff_text, "mutation 無效：未改到 ✅"
    problems = check_handoff_smoke_row(mutated_text) + check_gate(evidence, mutated_text)
    assert problems, "假綠：✅ 改回 ❌ 後守護未翻紅"


def test_black_sample_drop_event_field_turns_red(evidence, handoff_text):
    """抽掉證據 event 欄 → 無法自證由 release: published 觸發，守護翻紅。"""
    mutated = copy.deepcopy(evidence)
    mutated.pop("event", None)
    problems = check_evidence(mutated) + check_gate(mutated, handoff_text)
    assert problems, "假綠：抽掉 event 欄後守護未翻紅"


def test_black_sample_drop_failure_run_turns_red(evidence):
    """抽掉 superseded_failure_run → 只報喜藏憂，守護翻紅。"""
    mutated = copy.deepcopy(evidence)
    mutated.pop("superseded_failure_run", None)
    problems = check_evidence(mutated)
    assert any("superseded_failure_run" in p or "failure" in p for p in problems), (
        f"假綠：抽掉 failure run 後守護未翻紅，problems={problems}"
    )


def test_black_sample_pending_but_still_green_turns_red(evidence, handoff_text):
    """verification_status 退回 pending 但 handoff 仍 ✅ → gate 翻紅（誠實性核心）。"""
    mutated = copy.deepcopy(evidence)
    mutated["verification_status"] = "pending"
    problems = check_gate(mutated, handoff_text)
    assert any("假綠" in p for p in problems), (
        f"假綠：pending 卻仍 ✅ 未被擋下，problems={problems}"
    )
