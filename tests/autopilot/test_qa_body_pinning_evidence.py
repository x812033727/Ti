"""QA 任務 #1：release body 頂部 Breaking 置頂證據守門測試。

守護 `docs/release-e2e-handoff.md` 的 body 列與兩份證據：
  - `docs/evidence/release-v0.2.0-online-body.json`
  - `docs/evidence/release-v0.2.0-body-structure-verdict.json`

核心不變式：
  1. 線上 body 證據須為 `body_match=true`，且 `body_sha256` 具固定長度。
  2. verdict 須明示 `PASS`，並回指同一份 source_evidence / checker_script。
  3. handoff 的 body 列必須已翻 ✅，且同列同時帶到 evidence / verdict / script / 守護測試路徑。

本檔與 smoke 證據測試對稱：用同一把尺檢查「證據、runbook、守護」三者是否真的閉環，
避免文件只看起來有回指、實際卻沒綁定。
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
EVIDENCE_JSON = ROOT / "docs" / "evidence" / "release-v0.2.0-online-body.json"
VERDICT_JSON = ROOT / "docs" / "evidence" / "release-v0.2.0-body-structure-verdict.json"
HANDOFF_MD = ROOT / "docs" / "release-e2e-handoff.md"
CHECK_SCRIPT = ROOT / "scripts" / "check_release_body_structure.py"

EXPECTED_EVIDENCE_PATH = "docs/evidence/release-v0.2.0-online-body.json"
EXPECTED_VERDICT_PATH = "docs/evidence/release-v0.2.0-body-structure-verdict.json"
EXPECTED_SCRIPT_PATH = "scripts/check_release_body_structure.py"
EXPECTED_GUARD_REF = (
    "tests/autopilot/test_qa_body_pinning_evidence.py::"
    "test_handoff_body_row_is_green_with_evidence_paths"
)


@pytest.fixture(scope="module")
def evidence() -> dict:
    assert EVIDENCE_JSON.exists(), f"前提失效：缺證據檔 {EVIDENCE_JSON}"
    return json.loads(EVIDENCE_JSON.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def verdict() -> dict:
    assert VERDICT_JSON.exists(), f"前提失效：缺判定檔 {VERDICT_JSON}"
    return json.loads(VERDICT_JSON.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def handoff_text() -> str:
    assert HANDOFF_MD.exists(), f"前提失效：缺移交文件 {HANDOFF_MD}"
    return HANDOFF_MD.read_text(encoding="utf-8")


def _normalize(text: str) -> str:
    lines = [ln.rstrip() for ln in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines)


def _first_top_h2(body: str) -> str | None:
    for raw in body.splitlines():
        line = raw.rstrip()
        if line.startswith("## "):
            return line
    return None


def _body_row(text: str) -> str:
    for line in text.splitlines():
        if line.lstrip().startswith("|") and EXPECTED_EVIDENCE_PATH in line:
            return line
    return ""


def check_body_evidence(obj: dict, verdict_obj: dict) -> list[str]:
    problems: list[str] = []

    if obj.get("body_match") is not True:
        problems.append("body_match 非 true（線上 body 未證成）")
    if obj.get("tag_match") is not True:
        problems.append("tag_match 非 true")
    if obj.get("url_match") is not True:
        problems.append("url_match 非 true")

    body_sha256 = obj.get("body_sha256") or ""
    if not re.fullmatch(r"[0-9a-f]{64}", body_sha256):
        problems.append("body_sha256 缺失或非 64 碼 hex")

    gh_body = ((obj.get("gh_release_view") or {}).get("body")) or ""
    rest_body = ((obj.get("rest_release_by_tag_subset") or {}).get("body")) or ""
    if not gh_body or not rest_body:
        problems.append("gh/rest body 缺失")
    elif _normalize(gh_body) != _normalize(rest_body):
        problems.append("gh/rest body 正規化後不相等")

    first_h2 = _first_top_h2(_normalize(gh_body))
    if first_h2 != "## ⚠️ Breaking Changes":
        problems.append("線上 body 頂部第一個頂層 h2 非 Breaking Changes")
    if "TI_REQUIRE_CHOWN=warn" not in gh_body or "TI_REQUIRE_CHOWN=off" not in gh_body:
        problems.append("線上 body 缺逃生艙字串 `TI_REQUIRE_CHOWN=warn/off`")

    if verdict_obj.get("verdict") != "PASS":
        problems.append("verdict 非 PASS")
    if verdict_obj.get("source_evidence") != EXPECTED_EVIDENCE_PATH:
        problems.append("verdict.source_evidence 未回指同一份 online-body.json")
    if verdict_obj.get("checker_script") != EXPECTED_SCRIPT_PATH:
        problems.append("verdict.checker_script 未回指 check_release_body_structure.py")

    checks = verdict_obj.get("checks") or {}
    for key in (
        "雙來源正規化後逐字相等(gh vs REST)",
        "頂部即 Breaking 置頂",
        "四要素齊(①行為變動②原因③before/after④生效版本)",
        "生效版本逐字對應_自0.2.0起",
        "逃生艙_TI_REQUIRE_CHOWN=warn/off",
    ):
        if checks.get(key) is not True:
            problems.append(f"verdict checks.{key} 非 true")

    if not CHECK_SCRIPT.exists():
        problems.append(f"缺核對腳本 {CHECK_SCRIPT}")

    return problems


def check_handoff_body_row(text: str) -> list[str]:
    problems: list[str] = []
    row = _body_row(text)
    if not row:
        problems.append("找不到 handoff 的 body 置頂列")
        return problems

    if "✅" not in row:
        problems.append("body 置頂列未標 ✅")
    if "⏳" in row:
        problems.append("body 置頂列仍含 ⏳（未翻封）")
    if "❌" in row:
        problems.append("body 置頂列仍含 ❌（未翻正）")

    for token, msg in (
        (EXPECTED_EVIDENCE_PATH, "body 置頂列未回指 online-body.json"),
        (EXPECTED_VERDICT_PATH, "body 置頂列未回指 body-structure-verdict.json"),
        (EXPECTED_SCRIPT_PATH, "body 置頂列未回指 check_release_body_structure.py"),
        ("body_match=true", "body 置頂列未標 body_match=true"),
        ("body_sha256", "body 置頂列未提 body_sha256"),
        ("verdict=PASS", "body 置頂列未提 verdict=PASS"),
        (EXPECTED_GUARD_REF, "body 置頂列未回指守護測試"),
    ):
        if token not in row:
            problems.append(msg)

    return problems


def check_gate(obj: dict, verdict_obj: dict, text: str) -> list[str]:
    problems: list[str] = []
    row = _body_row(text)
    row_green = bool(row) and "✅" in row and "⏳" not in row and "❌" not in row
    evidence_ok = not check_body_evidence(obj, verdict_obj)

    if row_green and not evidence_ok:
        problems.append("handoff body 列標 ✅ 但證據未達 PASS")
    if evidence_ok and not row_green:
        problems.append("證據已 PASS 但 handoff body 列未翻 ✅")

    if row and EXPECTED_EVIDENCE_PATH in row and obj.get("body_match") is not True:
        problems.append("body 列引用了證據，但 body_match 未通過")

    return problems


def test_body_evidence_is_verified_and_pinned(evidence, verdict):
    problems = check_body_evidence(evidence, verdict)
    assert problems == [], "body 證據缺漏：\n  - " + "\n  - ".join(problems)


def test_handoff_body_row_is_green_with_evidence_paths(evidence, verdict, handoff_text):
    problems = check_handoff_body_row(handoff_text) + check_gate(evidence, verdict, handoff_text)
    assert problems == [], "handoff body 列缺漏：\n  - " + "\n  - ".join(problems)


def test_mutation_drop_body_match_turns_red(evidence, verdict, handoff_text):
    mutated = copy.deepcopy(evidence)
    mutated["body_match"] = False
    problems = check_body_evidence(mutated, verdict) + check_gate(mutated, verdict, handoff_text)
    assert problems, "假綠：body_match 改 False 後守護未翻紅"


def test_mutation_relabel_body_row_to_pending_turns_red(evidence, verdict, handoff_text):
    row = _body_row(handoff_text)
    assert "✅" in row, "baseline 失效：body 列原本就無 ✅"
    mutated_text = handoff_text.replace(row, row.replace("✅", "⏳"))
    assert mutated_text != handoff_text, "mutation 無效：未改到 body 列"
    problems = check_handoff_body_row(mutated_text) + check_gate(evidence, verdict, mutated_text)
    assert problems, "假綠：body 列改成 ⏳ 後守護未翻紅"


def test_mutation_drop_checker_script_path_turns_red(evidence, verdict, handoff_text):
    row = _body_row(handoff_text)
    assert EXPECTED_SCRIPT_PATH in row, "baseline 失效：body 列原本就無 script path"
    mutated_text = handoff_text.replace(EXPECTED_SCRIPT_PATH, "scripts/check_release_body_structure_old.py")
    assert mutated_text != handoff_text, "mutation 無效：未改到 script path"
    problems = check_handoff_body_row(mutated_text) + check_gate(evidence, verdict, mutated_text)
    assert any("script" in p or "回指" in p for p in problems), (
        f"假綠：script path 拿掉後守護未翻紅，problems={problems}"
    )
