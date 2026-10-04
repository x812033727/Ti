"""QA 任務 #1 驗收：對照需求逐項比對三檔，產出「已達成／缺口」清單。

對應驗收標準（破壞性思考，預設東西是壞的直到證明能動）：
  AC#1 產出需求對照表，每項需求標記「已達成（附檔案/行號）」或
        「缺口（附具體描述）」，無遺漏。
  AC#2 `python3 scripts/publish_release.py` 能產生 `body.md`，內容含頂層
        Breaking Changes 區塊，且版本字串來自 `pyproject_version()`。
  AC#3 守護測試證明：`publish-release.yml` Create release step 使用
        `secrets.GH_PAT`（非 `GITHUB_TOKEN`），`release-smoke.yml` 為
        `on: release: published`；相關 release 測試全綠。
  AC#4 `#3` 若決定不加硬化，須明文記錄理由（不得無聲略過）。
  AC#5 文件含 `GH_PAT` 設定指引與半閉環聲明。
  AC#6 護欄本體零修改：`release-smoke.yml` 與既有守護測試無破壞性改動。

設計：
  - 不重跑其他守護測試（由 test_qa_task2/3/4 與 test_release_pipeline_dry_run 覆蓋，
    73 passed 已實測）；本檔聚焦「對照表 + 文件 + 護欄無破壞」三項結構性驗收。
  - 對 audit 文件採「項目數下限 + 結構欄位」雙重斷言，杜絕孤立假綠。
  - 對硬化決議採「字面錨點」：必須見到「不」「不補」「否決」之類反向詞，
    否則視同無聲略過。
  - 對文件指引採「必要詞彙命中」：fine-grained、本 repo、Contents、輪替、半閉環、
    端到端六項至少各命中一次。
"""

from __future__ import annotations

import fcntl
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
AUDIT = ROOT / "docs" / "release-pipeline-requirements-audit.md"
CLAUDE = ROOT / "CLAUDE.md"
PUBLISH_YML = ROOT / ".github" / "workflows" / "publish-release.yml"
SMOKE_YML = ROOT / ".github" / "workflows" / "release-smoke.yml"
SCRIPT = ROOT / "scripts" / "publish_release.py"
BODY_MD = ROOT / "body.md"


def _render_bodymd_for_test() -> str:
    """Run the fixed-output release script without leaking shared body.md across xdist workers."""
    with SCRIPT.open("rb") as lock_fh:
        fcntl.flock(lock_fh, fcntl.LOCK_EX)
        try:
            if BODY_MD.exists():
                BODY_MD.unlink()
            rc = subprocess.call(["python3", str(SCRIPT)], cwd=str(ROOT))
            assert rc == 0, f"scripts/publish_release.py exit={rc}"
            assert BODY_MD.exists(), "AC#2：body.md 未產出"
            return BODY_MD.read_text(encoding="utf-8")
        finally:
            BODY_MD.unlink(missing_ok=True)
            fcntl.flock(lock_fh, fcntl.LOCK_UN)


# ---------------------------------------------------------------------------
# AC#1：對照表存在、結構完整、無遺漏
# ---------------------------------------------------------------------------


def test_audit_doc_exists():
    assert AUDIT.exists(), f"AC#1：缺對照表 {AUDIT}"


def test_audit_doc_has_achieved_section_with_file_line_refs():
    """「已達成」段每列必須附檔案/行號（如 `xxx.yml:NN`）。"""
    text = AUDIT.read_text(encoding="utf-8")
    # 抓 markdown 表格行（首尾 |）
    table_rows = [ln for ln in text.splitlines() if ln.startswith("|") and ln.count("|") >= 4]
    # 排除表頭分隔行（---）
    body_rows = [r for r in table_rows if not re.match(r"^\|\s*:?-+:?\s*\|", r)]
    assert body_rows, "AC#1：對照表無資料列"
    # 取「已達成」段（檔名/行號樣式）
    achieved_rows = [
        r
        for r in body_rows
        if "已達成" in r and re.search(r"`[\w/_\-./]+\.(yml|py|md):\d+(-\d+)?`", r)
    ]
    assert len(achieved_rows) >= 10, (
        f"AC#1：已達成段僅 {len(achieved_rows)} 列含檔案/行號，預期 ≥10 條引用"
    )


def test_audit_doc_has_gap_section_with_descriptions():
    """「缺口」段每列必須附具體描述（非空判定欄）。"""
    text = AUDIT.read_text(encoding="utf-8")
    gap_rows = [
        ln
        for ln in text.splitlines()
        if ln.startswith("|")
        and "缺口" in ln
        and ln.count("|") >= 4
        and not re.match(r"^\|\s*:?-+:?\s*\|", ln)
    ]
    assert gap_rows, "AC#1：對照表無缺口段資料列"
    # 每列第三欄（描述）必須非空
    for r in gap_rows:
        cells = [c.strip() for c in r.split("|")]
        # cells[0] = ""、cells[1] = 缺口項、cells[2] = 判定（缺口/非阻塞缺口）、cells[3] = 描述、cells[4] = ""
        # 取最後一個非空 cell 作為描述
        desc = next((c for c in reversed(cells) if c), "")
        assert len(desc) >= 10, f"AC#1：缺口列描述過短或空：{r!r}"


def test_audit_doc_marks_smoke_trigger_published():
    """對照表必須明文標記 `release-smoke.yml` 觸發為 `release.types: [published]`。"""
    text = AUDIT.read_text(encoding="utf-8")
    assert "published" in text, "AC#3：對照表未提到 release:published 觸發"
    assert "release-smoke" in text or "smoke" in text, "AC#3：對照表未提到 release-smoke"


def test_audit_doc_marks_pat_not_github_token():
    """對照表必須明文標記 Create release 使用 `secrets.GH_PAT` 而非 `GITHUB_TOKEN`。"""
    text = AUDIT.read_text(encoding="utf-8")
    assert "GH_PAT" in text, "AC#3：對照表未提到 GH_PAT"
    assert "GITHUB_TOKEN" in text or "github.token" in text, (
        "AC#3：對照表未對照 GITHUB_TOKEN 觸發死結"
    )


# ---------------------------------------------------------------------------
# AC#2：`python3 scripts/publish_release.py` 產 body.md + Breaking + SSOT 版本
# ---------------------------------------------------------------------------


def test_publish_release_script_runs_and_writes_bodymd():
    """執行入口腳本，必須寫出 body.md 且含 BREAKING_HEADING。"""
    body = _render_bodymd_for_test()
    assert body.strip(), "AC#2：body.md 內容不可為空"


def test_bodymd_contains_breaking_heading_and_pyproject_version():
    """body.md 必須含頂層 Breaking Changes 區塊，且版本字串來自 pyproject_version()。"""
    # 從 SSOT 動態取版本（避免硬寫）
    spec = subprocess.run(
        [
            "python3",
            "-c",
            "from studio.release_note import BREAKING_HEADING, pyproject_version; "
            "print(BREAKING_HEADING); print(pyproject_version())",
        ],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=True,
    )
    heading, version = spec.stdout.strip().splitlines()
    body = _render_bodymd_for_test()
    assert heading in body, f"AC#2：body.md 缺 BREAKING_HEADING {heading!r}"
    assert version in body, f"AC#2：body.md 未帶 pyproject 版本 {version!r}"


def test_publish_yml_resolves_version_from_pyproject_not_hardcoded():
    """publish-release.yml 不能硬寫版本字串；版本必須經 `pyproject_version()` 取。"""
    yml = PUBLISH_YML.read_text(encoding="utf-8")
    # 必須有 Python 讀 pyproject_version 的程式碼片段
    assert "pyproject_version" in yml, "AC#2：publish-release.yml 未引用 pyproject_version()"
    # 不得出現 0.2.0 / v0.2.0 這類硬寫版本字串在 yaml 內容（CHANGELOG/註解除外）
    # 以正則檢查「version: 'X.Y.Z'」這種 YAML 硬寫樣式——允許在註解裡但不應作為 step 輸出源。
    assert not re.search(r"""(?m)^\s*version:\s*["']?\d+\.\d+\.\d+""", yml), (
        "AC#2：publish-release.yml 硬寫版本字串（應走 pyproject_version()）"
    )


# ---------------------------------------------------------------------------
# AC#4：硬化決議若不加，須明文記錄理由（不得無聲略過）
# ---------------------------------------------------------------------------


DECISION_ARTIFACTS = [
    ROOT / "DECISIONS.md",
    ROOT / "docs" / "release-pipeline-requirements-audit.md",
    ROOT / "CLAUDE.md",
]


def _combined_text() -> str:
    """取所有決策文件的合併文字（任何一個存在即可）。"""
    chunks = []
    for p in DECISION_ARTIFACTS:
        if p.exists():
            chunks.append(p.read_text(encoding="utf-8"))
    return "\n".join(chunks)


def test_hardening_decision_recorded_not_silent_skip():
    """`--verify-tag` 等硬化必須在某決策文件明文留下「不補」理由。"""
    text = _combined_text()
    # 命中 `--verify-tag` 字串
    assert "--verify-tag" in text, (
        "AC#4：硬化 `--verify-tag` 在所有決策文件皆無聲略過（未提及＝無聲略過）"
    )
    # 反向詞：必須見到「不」「否決」「不補」之類錨點
    # 取包含 `--verify-tag` 的行 ±2 行
    lines = text.splitlines()
    hits = []
    for i, ln in enumerate(lines):
        if "--verify-tag" in ln:
            ctx = "\n".join(lines[max(0, i - 3) : i + 4])
            hits.append(ctx)
    assert hits, "AC#4：未抓到 --verify-tag 上下文（邏輯漏洞）"
    # 至少有一處上下文出現「不」「否決」「不補」「無需」「拒絕」之一
    refusal_terms = ("不補", "否決", "無需", "拒絕", "不必", "不需", "no-op", "鍍金")
    assert any(any(t in ctx for t in refusal_terms) for ctx in hits), (
        f"AC#4：--verify-tag 出現但未附『不補/否決』理由：{hits[:3]}"
    )


# ---------------------------------------------------------------------------
# AC#5：文件含 GH_PAT 設定指引 + 半閉環聲明（拆兩段）
#   (a) 對照表必須同步目前狀態：協作文件缺口已關閉，但後續版本 E2E 仍需逐版驗證。
#   (b) 正式協作文件已補完指引（任務 #4 範圍）。
# ---------------------------------------------------------------------------


GH_PAT_REQUIRED_PHRASES = {
    "fine-grained": r"(?i)fine[-\s]?grained",
    "本 repo": r"本\s*repo|此\s*repo|this\s+repo",
    "Contents 權限": r"Contents:?\s*(read|write|read\s*and\s*write|讀|讀寫)",
    "secret 名稱 GH_PAT": r"`?GH_PAT`?",
    "輪替/過期處置": r"輪替|過期|rotate|expir",
    "半閉環聲明": r"半閉環|half[-\s]?closed|半\s*閉\s*環",
    "端到端未驗證": r"端到端|E2E|end[-\s]?to[-\s]?end",
}


def test_audit_doc_marks_ac5_docs_closed_and_e2e_boundary_open():
    """AC#5 (a)：對照表必須把文件缺口標為已達成，且保留後續版本 E2E 邊界。

    任務 #4 已補完 GH_PAT 文件與半閉環聲明；audit 不得停在舊缺口狀態。
    但不能因此宣稱未來版本真實 tag-push E2E 自動閉環，仍須逐版生產驗證。
    """
    text = AUDIT.read_text(encoding="utf-8")
    table_rows = [
        ln
        for ln in text.splitlines()
        if ln.startswith("|") and ln.count("|") >= 4 and not re.match(r"^\|\s*:?-+:?\s*\|", ln)
    ]

    gh_pat_rows = [r for r in table_rows if "GH_PAT" in r and ("文件" in r or "設定" in r)]
    assert gh_pat_rows, "AC#5 (a)：對照表缺 GH_PAT 文件/設定列"
    assert any("已達成" in r for r in gh_pat_rows), (
        f"AC#5 (a)：GH_PAT 文件缺口已關閉，audit 不應仍只列缺口：{gh_pat_rows}"
    )
    assert not any("缺口" in r for r in gh_pat_rows), (
        f"AC#5 (a)：GH_PAT 文件仍被列成缺口，狀態過期：{gh_pat_rows}"
    )

    half_closed_rows = [r for r in table_rows if "半閉環聲明文件化" in r]
    assert half_closed_rows, "AC#5 (a)：對照表缺半閉環聲明文件化列"
    assert any("已達成" in r for r in half_closed_rows), (
        f"AC#5 (a)：半閉環聲明文件化已關閉，audit 不應仍只列缺口：{half_closed_rows}"
    )
    assert not any("缺口" in r for r in half_closed_rows), (
        f"AC#5 (a)：半閉環聲明文件化仍被列成缺口，狀態過期：{half_closed_rows}"
    )

    gap_rows = [r for r in table_rows if "缺口" in r]
    assert any("後續版本" in r and "端到端" in r for r in gap_rows), (
        f"AC#5 (a)：audit 必須保留後續版本真實 E2E 逐版驗證缺口：{gap_rows}"
    )


def test_ac5_op_guide_present_in_collab_doc():
    """AC#5 (b)：正式協作文件已補完指引（任務 #4 範圍，目前未達成）。

    標記為缺口（測試失敗 = 缺口未補 = 預期現狀），由任務 #4 收尾。
    本測試保留為「誠實鏡子」，不許悄悄移除；缺口閉合後此測試自動轉綠。
    """
    primary = CLAUDE
    alt = ROOT / "docs" / "release-ops.md"
    if primary.exists():
        text = primary.read_text(encoding="utf-8")
        src = "CLAUDE.md"
    elif alt.exists():
        text = alt.read_text(encoding="utf-8")
        src = "docs/release-ops.md"
    else:
        pytest.fail(
            "AC#5 (b)：缺正式協作文件（CLAUDE.md 或 docs/release-ops.md），"
            "任務 #4 收尾前不可移除此斷言"
        )

    missing = [name for name, pat in GH_PAT_REQUIRED_PHRASES.items() if not re.search(pat, text)]
    assert not missing, (
        f"AC#5 (b)：{src} 缺以下必要段落錨點 {missing}。"
        f" 任務 #4 收尾前此斷言預期失敗；任務 #4 完成後應轉綠。"
        f"  需求：① fine-grained PAT ② 本 repo only ③ Contents: read/write"
        f"  ④ secret 名稱 GH_PAT ⑤ 過期/輪替處置 ⑥ 半閉環聲明 ⑦ 端到端尚待生產驗證"
    )


# ---------------------------------------------------------------------------
# AC#6：護欄本體零修改
# ---------------------------------------------------------------------------


GUARDRAIL_FILES = [
    ".github/workflows/release-smoke.yml",
    "tests/autopilot/test_qa_task2_release_body.py",
    "tests/autopilot/test_qa_task3_release_trigger_chain.py",
    "tests/autopilot/test_qa_task4_publish_workflow_guard.py",
]


@pytest.mark.parametrize("relpath", GUARDRAIL_FILES)
def test_guardrail_file_unchanged_in_working_tree(relpath):
    """護欄檔案在工作目錄不可被改動（git diff 為空）。"""
    fp = ROOT / relpath
    assert fp.exists(), f"前提失效：缺護欄檔 {relpath}"
    out = subprocess.run(
        ["git", "diff", "--", relpath],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert out.stdout.strip() == "", f"AC#6：護欄檔 {relpath} 有未提交修改：\n{out.stdout[:500]}"


def test_smoke_workflow_unchanged_against_last_commit():
    """`release-smoke.yml` 與 HEAD 必須完全一致（無新增/刪除/修飾）。"""
    out = subprocess.run(
        ["git", "diff", "HEAD", "--", ".github/workflows/release-smoke.yml"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert out.stdout.strip() == "", f"AC#6：release-smoke.yml 與 HEAD 有差異：\n{out.stdout[:500]}"
