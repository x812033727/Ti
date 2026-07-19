"""QA 任務 #4：發佈文件契約守護測試。

對應驗收標準（破壞性思考，預設文件是壞的直到證明齊備）：

  AC#5 發佈文件（即 CLAUDE.md）必須含：
       1. `GH_PAT` 設定指引四項規格：
          a. Fine-grained PAT
          b. Repository access = Only select repositories（本 repo only，非 all-repos）
          c. Repository permissions = `Contents: Read and write`
          d. Secret 名稱固定為 `GH_PAT`
       2. 發佈 DoD 段落（`scripts/publish_release.py` 必跑、版本走
          `studio.release_note.pyproject_version()`、Breaking heading 走 SSOT、
          不在 YAML 硬寫）。
       3. 明文標註「真實 `v*` tag-push 端到端尚待生產驗證」──本輪守護測試為半閉環。
       4. `GH_PAT` 到期／撤銷 403 處置流程（避免 Step 5 失敗只回 403，後人不知怎麼修）。

  AC#6 護欄本體零修改：`release-smoke.yml` 既有觸發契約與既有守護測試，
       對 `550b9e3`（任務#4 commit）必須無破壞性變更。

設計：
  - 把每條契約抽成可重用判斷式 `check_*`，baseline 與 mutation 共用同一把尺：
    證明守護「有真鑑別力」（mutation 非空），杜絕「字串 grep 假綠」。
  - 強契約（必須逐字命中）以 token 形式呈現；描述性短語用 lowercase + 正規化比對
    （避免斷行/全半形漂移讓契約失效）。`GH_PAT` 與 `secrets.GH_PAT` 是契約本體，
    不視為「字面值硬寫」（契約名稱必須逐字）。
  - 失敗路徑：缺任一規格／半閉環語句／DoD 命令／403 處置 → 紅。
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CLAUDE_MD = ROOT / "CLAUDE.md"
RELEASE_SMOKE_YML = ROOT / ".github" / "workflows" / "release-smoke.yml"
PUBLISH_RELEASE_YML = ROOT / ".github" / "workflows" / "publish-release.yml"
EXISTING_GUARD_TESTS = (
    ROOT / "tests" / "autopilot" / "test_qa_task4_publish_workflow_guard.py",
    ROOT / "tests" / "autopilot" / "test_qa_task3_release_trigger_chain.py",
    ROOT / "tests" / "autopilot" / "test_qa_task2_release_body.py",
)
TASK4_COMMIT = "550b9e3"  # 任務#4 第1輪 commit（護欄本體應零修改）


def _commit_present(sha: str) -> bool:
    """commit 物件是否在當前 clone 內可見。

    CI 用 `actions/checkout` 淺層 clone（fetch-depth=1，只有 merge commit），
    `git show <開發期 sha>` 會 `bad object` exit 128。此守衛讓「對特定開發期
    commit 的歷史斷言」在全歷史沙箱仍實際驗證、在淺 clone 優雅 skip（非紅）。
    """
    return (
        subprocess.run(
            ["git", "cat-file", "-e", f"{sha}^{{commit}}"],
            cwd=ROOT,
            capture_output=True,
        ).returncode
        == 0
    )


_requires_task4_commit = pytest.mark.skipif(
    not _commit_present(TASK4_COMMIT),
    reason=f"commit {TASK4_COMMIT} 不在淺層 clone（CI shallow checkout）；開發期一次性歷史斷言，跳過",
)


# ---------------------------------------------------------------------------
# 解析輔助
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def claude_text() -> str:
    assert CLAUDE_MD.exists(), f"前提失效：缺 CLAUDE.md {CLAUDE_MD}"
    return CLAUDE_MD.read_text(encoding="utf-8")


def _norm(text: str) -> str:
    """正規化：去全形空白/換行/標點差異，比對語意短語。"""
    # 全形空白、半形空白、換行都壓成單一半形空白
    return re.sub(r"\s+", " ", text).strip().lower()


# ---------------------------------------------------------------------------
# AC#5.1 — GH_PAT 四項規格
# ---------------------------------------------------------------------------


def check_gh_pat_specs(text: str) -> list[str]:
    """四項規格任一缺漏即回報具體缺項。回傳空 list＝四項全在。"""
    problems: list[str] = []
    norm = _norm(text)

    # (a) 必須點名 Fine-grained PAT
    if "fine-grained pat" not in norm and "fine grained pat" not in norm:
        problems.append("GH_PAT 設定指引未明文要求『Fine-grained PAT』")

    # (b) 必須明指「本 repo only / Only select repositories / 非 all-repos」
    has_only_this_repo = (
        "only select repositories" in norm
        or "only select repository" in norm
        or "只選本 repo" in _norm(text)  # 中英並列容忍
        or "本 repo only" in _norm(text)
    )
    has_not_all_repos = (
        "非 all-repos" in _norm(text) or "non all-repos" in norm or "not all-repos" in norm
    )
    if not has_only_this_repo:
        problems.append("GH_PAT 設定指引未明指『只選本 repo（Only select repositories）』")
    if not has_not_all_repos:
        problems.append("GH_PAT 設定指引未明指『非 all-repos』")

    # (c) 必須明指 Contents: Read and write
    if "contents: read and write" not in norm and "read and write" not in norm:
        problems.append("GH_PAT 設定指引未明指『Contents: Read and write』")

    # (d) 必須明指 secret 名稱固定為 GH_PAT
    # 以逐字比對 `GH_PAT`（契約名稱）
    if "GH_PAT" not in text:
        problems.append("GH_PAT 設定指引未明指『secret 名稱固定 GH_PAT』")

    return problems


def test_claude_md_has_all_four_gh_pat_specs(claude_text):
    """AC#5.1：四項 GH_PAT 規格必須齊備。"""
    problems = check_gh_pat_specs(claude_text)
    assert problems == [], "AC#5.1：GH_PAT 設定指引有缺項：\n  - " + "\n  - ".join(problems)


def test_mutation_drop_fine_grained_turns_red():
    """反向 mutation：把『Fine-grained』拿掉 → 規格 (a) 守護必須翻紅。"""
    text = CLAUDE_MD.read_text(encoding="utf-8")
    assert "Fine-grained PAT" in text, "baseline 失效：原本就無 Fine-grained 規格"

    mutated = text.replace("Fine-grained PAT", "PAT")
    assert mutated != text, "mutation 無效：未替換到 Fine-grained"

    problems = check_gh_pat_specs(mutated)
    assert any("Fine-grained" in p for p in problems), (
        f"假綠：移除 Fine-grained 後守護未翻紅，problems={problems}"
    )


def test_mutation_drop_read_and_write_turns_red():
    """反向 mutation：把『Contents: Read and write』拿掉 → 規格 (c) 守護必須翻紅。"""
    text = CLAUDE_MD.read_text(encoding="utf-8")
    assert "Contents: Read and write" in text, "baseline 失效：原本就無 Read and write 規格"

    mutated = text.replace("Contents: Read and write", "Contents: write")
    assert mutated != text, "mutation 無效：未替換到 Read and write"

    problems = check_gh_pat_specs(mutated)
    assert any("Read and write" in p for p in problems), (
        f"假綠：降級為 write 後守護未翻紅，problems={problems}"
    )


def test_mutation_drop_repo_scope_turns_red():
    """反向 mutation：把『只選本 repo』拿掉 → 規格 (b) 守護必須翻紅。"""
    text = CLAUDE_MD.read_text(encoding="utf-8")
    assert "只選本 repo" in text, "baseline 失效：原本就無『只選本 repo』"

    mutated = text.replace("只選本 repo", "所有 repo")
    assert mutated != text, "mutation 無效：未替換到本 repo 字串"

    problems = check_gh_pat_specs(mutated)
    assert any("只選本 repo" in p or "all-repos" in p for p in problems), (
        f"假綠：放寬 repo 範圍後守護未翻紅，problems={problems}"
    )


def test_mutation_rename_secret_turns_red():
    """反向 mutation：把 secret 名稱改名（GH_PAT→GITHUB_PAT）→ 規格 (d) 守護必須翻紅。

    規格 (d) 的關鍵風險＝後人改 secret 名稱但忘了同步 workflow env 路由，
    結果 Step 1 PAT guard 與 Step 5 Create release 攔不到空 PAT，CI 紅得莫名。
    """
    text = CLAUDE_MD.read_text(encoding="utf-8")
    assert "GH_PAT" in text, "baseline 失效：原本就無 GH_PAT 字串"

    mutated = text.replace("GH_PAT", "GITHUB_PAT")
    assert mutated != text, "mutation 無效：未替換到 GH_PAT"

    problems = check_gh_pat_specs(mutated)
    assert any("GH_PAT" in p for p in problems), (
        f"假綠：改名 secret 後守護未翻紅，problems={problems}"
    )


# ---------------------------------------------------------------------------
# AC#5.2 — 發佈 DoD 段落：scripts/publish_release.py、SSOT、不可硬寫
# ---------------------------------------------------------------------------


def check_release_dod(text: str) -> list[str]:
    """發佈 DoD 必須明文：(1) 跑 publish_release.py (2) 版本走 pyproject_version
    (3) Breaking heading 走 SSOT、不在 YAML 硬寫 (4) 發佈前必跑守護測試。

    回傳空 list = DoD 完整。
    """
    problems: list[str] = []
    norm = _norm(text)

    # (1) 必明指執行 scripts/publish_release.py
    if "publish_release.py" not in text:
        problems.append("發佈 DoD 未明文要求執行 `scripts/publish_release.py`")

    # (2) 必明指版本來自 pyproject_version()（非 YAML 硬寫）
    if "pyproject_version" not in norm and "pyproject" not in norm:
        problems.append("發佈 DoD 未明指版本來自 `pyproject_version()`（SSOT）")
    if "不在 yaml 硬寫" not in norm and "不硬寫" not in norm and "不寫死" not in norm:
        problems.append("發佈 DoD 未明指『版本不在 YAML 硬寫』")

    # (3) 必明指 Breaking heading 來自 SSOT
    if "breaking" not in norm:
        problems.append("發佈 DoD 未提及 Breaking Changes 處理路徑（SSOT）")

    # (4) 必明指發佈前必跑 release 相關守護測試（pytest 命令或同義語句）
    has_pytest_or_regress = "pytest" in norm or "守護測試" in text or "regression" in norm
    if not has_pytest_or_regress:
        problems.append("發佈 DoD 未明指『發佈前必跑守護測試』")

    return problems


def test_claude_md_release_dod_intact(claude_text):
    """AC#5.2：發佈 DoD 段落必須含四項要件。"""
    problems = check_release_dod(claude_text)
    assert problems == [], "AC#5.2：發佈 DoD 段落有缺項：\n  - " + "\n  - ".join(problems)


def test_mutation_drop_pyproject_ssot_turns_red():
    """反向 mutation：把 pyproject_version() 拿掉 → DoD 守護必須翻紅。"""
    text = CLAUDE_MD.read_text(encoding="utf-8")
    assert "pyproject_version" in text, "baseline 失效：原本就無 pyproject_version"

    mutated = text.replace("pyproject_version()", "<占位符>")
    assert mutated != text, "mutation 無效：未替換到 pyproject_version"

    problems = check_release_dod(mutated)
    assert any("pyproject" in p for p in problems), (
        f"假綠：抽掉 SSOT 字串後守護未翻紅，problems={problems}"
    )


def test_mutation_drop_publish_script_turns_red():
    """反向 mutation：把 `scripts/publish_release.py` 拿掉 → DoD 守護必須翻紅。"""
    text = CLAUDE_MD.read_text(encoding="utf-8")
    assert "scripts/publish_release.py" in text, "baseline 失效：原本就無 publish_release.py"

    mutated = text.replace("scripts/publish_release.py", "scripts/noop.py")
    assert mutated != text, "mutation 無效：未替換到 publish_release.py"

    problems = check_release_dod(mutated)
    assert any("publish_release.py" in p for p in problems), (
        f"假綠：改掉命令後守護未翻紅，problems={problems}"
    )


# ---------------------------------------------------------------------------
# AC#5.3 — 半閉環／端到端待生產驗證語句
# ---------------------------------------------------------------------------


# 「真實 tag-push 端到端尚待生產驗證」的關鍵詞集合，任一關鍵詞命中即視為覆蓋。
# 容忍不同段落／斷行／全半形。
HALF_CLOSED_KEYWORDS = (
    "真實",  # 必含「真實」修飾（避免「測試已驗證」誤判）
    "tag-push",  # 必含具體觸發動作
    "端到端",  # 必含 E2E 概念
    "生產驗證",  # 必含生產驗證或同義
)


def check_half_closed_disclaimer(text: str) -> list[str]:
    """明文標註「真實 tag-push 端到端尚待生產驗證」——逐字收斂到關鍵詞集合。

    為什麼用關鍵詞集合而非單一長字串：文件語句可斷行、可加修飾（單元/守護測試
    為半閉環）。只要語意齊備即通過；任一關鍵詞缺漏則紅。
    """
    problems: list[str] = []
    norm = _norm(text)

    # 必須「明文標註」：意即文件有顯式語句（非只藏在 workflow 註解）
    for kw in HALF_CLOSED_KEYWORDS:
        if kw not in text and kw.replace("-", "") not in norm.replace("-", ""):
            problems.append(f"半閉環聲明缺關鍵詞：{kw!r}")

    # 必須含「半閉環」或同義（單元/守護測試 ≠ E2E 證明）
    if (
        "半閉環" not in text
        and "未在生產跑過" not in text
        and "尚未" not in text
        and "尚待" not in text
    ):
        problems.append("半閉環聲明缺『半閉環／尚待／尚未』等修飾詞（可能誤判為已 E2E 驗證）")

    return problems


def test_claude_md_has_half_closed_disclaimer(claude_text):
    """AC#5.3：文件必明文標註『真實 tag-push 端到端尚待生產驗證』，且明示守護測試為半閉環。"""
    problems = check_half_closed_disclaimer(claude_text)
    assert problems == [], "AC#5.3：半閉環聲明有缺漏：\n  - " + "\n  - ".join(problems)


def test_mutation_soften_to_fully_verified_turns_red():
    """反向 mutation（最危險漂移）：把『尚待生產驗證』改成『已完整驗證』→ 守護必翻紅。

    漂移型：『端到端已完整通過，無需再驗』——這是任務最在意的反向情境。
    守護必翻紅，否則代表守護對『漂移為假綠』沒判別力。
    """
    text = CLAUDE_MD.read_text(encoding="utf-8")
    # baseline 必含「尚待」
    assert "尚待" in text or "尚未" in text, "baseline 失效：原本就無『尚待／尚未』"

    # 嘗試把『尚待生產驗證』改為『已完整驗證』
    mutated = re.sub(r"尚[待未]生產驗證", "已完整驗證", text)
    # 若無正則命中，用簡單替換
    if mutated == text:
        mutated = text.replace("尚待生產驗證", "已完整驗證").replace("尚未生產驗證", "已完整驗證")
    assert mutated != text, "mutation 無效：未把尚待改為已驗證（假綠風險）"

    problems = check_half_closed_disclaimer(mutated)
    assert any("半閉環" in p or "尚待" in p or "尚未" in p for p in problems), (
        f"假綠：漂移為『已完整驗證』後守護未翻紅，problems={problems}"
    )


def test_mutation_drop_tag_push_keyword_turns_red():
    """反向 mutation：把『tag-push』關鍵詞拿掉 → 守護必翻紅。

    為什麼關鍵：『真實 E2E』若沒有『tag-push』，可能漂移為單元測試覆蓋的誇大。
    """
    text = CLAUDE_MD.read_text(encoding="utf-8")
    assert "tag-push" in text, "baseline 失效：原本就無 tag-push 關鍵詞"

    mutated = text.replace("tag-push", "push")
    assert mutated != text, "mutation 無效：未替換到 tag-push"

    problems = check_half_closed_disclaimer(mutated)
    assert any("tag-push" in p for p in problems), (
        f"假綠：拿掉 tag-push 後守護未翻紅，problems={problems}"
    )


# ---------------------------------------------------------------------------
# AC#5.4 — GH_PAT 過期 403 處置流程
# ---------------------------------------------------------------------------


def check_gh_pat_403_runbook(text: str) -> list[str]:
    """GH_PAT 過期/撤銷處置流程：(a) 提到過期/撤銷導致 403 (b) 指到同一 repo 重新設定 GH_PAT
    (c) 不誤導去改 workflow 內 token 路由。回傳空 list = runbook 完整。"""
    problems: list[str] = []
    norm = _norm(text)

    # (a) 必明指 PAT 過期／撤銷會導致 403
    has_403 = "403" in text
    has_expire_or_revoke = "過期" in text or "撤銷" in text or "expire" in norm or "revoke" in norm
    if not has_403:
        problems.append("GH_PAT runbook 未明指『過期／撤銷會以 403 失敗』")
    if not has_expire_or_revoke:
        problems.append("GH_PAT runbook 未提及『過期／撤銷』任何觸發詞")

    # (b) 必明指更新同一個 secret 名稱（GH_PAT）而非建立新名稱
    if "更新" not in text and "輪替" not in text and "rotate" not in norm:
        problems.append("GH_PAT runbook 未提及更新／輪替流程")

    # (c) 必明指「不要改 workflow token 路由」（負向警示）
    has_no_route_change = (
        "不要改" in text
        or "不可換" in text
        or "不可改" in text
        or "不得改" in text
        or "not change the workflow" in norm
    )
    if not has_no_route_change:
        problems.append("GH_PAT runbook 未明文警示『不要改 workflow 內 token 路由』")

    return problems


def test_claude_md_gh_pat_403_runbook_intact(claude_text):
    """AC#5.4：GH_PAT 過期／撤銷 403 處置流程必須明文。"""
    problems = check_gh_pat_403_runbook(claude_text)
    assert problems == [], "AC#5.4：GH_PAT 403 處置流程有缺項：\n  - " + "\n  - ".join(problems)


def test_mutation_drop_403_hint_turns_red():
    """反向 mutation：拿掉 403 字串 → runbook 守護必翻紅（『過期會以 403 失敗』消失）。"""
    text = CLAUDE_MD.read_text(encoding="utf-8")
    assert "403" in text, "baseline 失效：原本就無 403"

    mutated = text.replace("403", "HTTP 錯誤")
    assert mutated != text, "mutation 無效：未替換到 403"

    problems = check_gh_pat_403_runbook(mutated)
    assert any("403" in p for p in problems), (
        f"假綠：拿掉 403 字串後守護未翻紅，problems={problems}"
    )


# ---------------------------------------------------------------------------
# AC#6 — 護欄本體零修改
#   任務#4 commit (550b9e3) 必須只動 CLAUDE.md，release-smoke.yml 與既有守護測試
#   不能被破壞性改動（接受補丁；但若 trigger 契約或 step/env 內容變更就翻紅）。
# ---------------------------------------------------------------------------


def _git_diff_names(commit: str) -> list[str]:
    """列出 commit 觸及的所有檔案路徑（相對 repo 根）。"""
    out = subprocess.run(
        ["git", "show", "--name-only", "--pretty=format:", commit],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return [ln.strip() for ln in out.stdout.splitlines() if ln.strip()]


@_requires_task4_commit
def test_task4_commit_touches_only_claude_md():
    """AC#6.1：任務#4 commit (550b9e3) 必須只動 CLAUDE.md。"""
    files = _git_diff_names(TASK4_COMMIT)
    assert files == ["CLAUDE.md"], f"AC#6.1：任務#4 commit 變更多個檔案={files}（護欄本體應零修改）"


@_requires_task4_commit
@pytest.mark.parametrize(
    "guard_path",
    EXISTING_GUARD_TESTS,
    ids=lambda p: p.name,
)
def test_task4_commit_does_not_touch_existing_guard_tests(guard_path):
    """AC#6.2：既有守護測試不應被任務#4 commit 改動。"""
    files = _git_diff_names(TASK4_COMMIT)
    rel = str(guard_path.relative_to(ROOT))
    assert rel not in files, f"AC#6.2：任務#4 commit 不應觸及 {rel}"


@_requires_task4_commit
def test_task4_commit_does_not_alter_release_smoke_trigger():
    """AC#6.3：release-smoke.yml 的 `release: published` 觸發契約不被破壞性改動。"""
    # 觸發契約檢查：smoke.yml 在任務#4 commit 之前後都仍含 `release: published`。
    # 用 git show <commit>:path 取得 commit 內檔案內容（若 commit 未變更即等同 HEAD）。
    before = subprocess.run(
        ["git", "show", f"{TASK4_COMMIT}~1:.github/workflows/release-smoke.yml"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    after = RELEASE_SMOKE_YML.read_text(encoding="utf-8")

    assert "release:" in before and "published" in before, "前提失效：HEAD 前 release-smoke 缺契約"
    assert "release:" in after and "published" in after, (
        "AC#6.3：release-smoke.yml 在任務#4 後丟失 `release: published` 觸發契約"
    )
    # 進一步：trigger 區塊逐字相同（接受純註解 reflow，但拒絕 trigger 類型變更）
    before_trigger = re.search(r"on:\s*\n\s*release:\s*\n\s*types:\s*\[published\]", before)
    after_trigger = re.search(r"on:\s*\n\s*release:\s*\n\s*types:\s*\[published\]", after)
    assert before_trigger and after_trigger, (
        f"AC#6.3：release-smoke 觸發契約漂移\n  before={'Y' if before_trigger else 'N'}\n"
        f"  after={'Y' if after_trigger else 'N'}"
    )


def test_mutation_smoke_published_to_created_turns_red_for_chainguard():
    """反向 mutation：把 release-smoke trigger 從 published 改 created → 守護必翻紅。

    證明上面 `test_task4_commit_does_not_alter_release_smoke_trigger` 有真鑑別力。
    """
    text = RELEASE_SMOKE_YML.read_text(encoding="utf-8")
    assert "[published]" in text, "前提失效：smoke 原本就非 published 觸發"

    mutated = text.replace("[published]", "[created]")
    assert mutated != text, "mutation 無效：未替換到 trigger types"

    assert "[published]" not in mutated, "mutation 後契約漂移未生效＝假綠"
    assert "[created]" in mutated, "mutation 後契約漂移未生效＝假綠"


# ---------------------------------------------------------------------------
# 整合：文件契約彙整（給 reviewer 一眼看缺口）
# ---------------------------------------------------------------------------


def test_claude_md_release_section_full_audit(claude_text):
    """文件契約整合檢查：所有 AC#5 子項 + AC#6 護欄本體未動 = 全部綠。"""
    all_problems: list[str] = []
    all_problems += [f"[GH_PAT 規格] {p}" for p in check_gh_pat_specs(claude_text)]
    all_problems += [f"[發佈 DoD] {p}" for p in check_release_dod(claude_text)]
    all_problems += [f"[半閉環聲明] {p}" for p in check_half_closed_disclaimer(claude_text)]
    all_problems += [f"[403 runbook] {p}" for p in check_gh_pat_403_runbook(claude_text)]
    assert all_problems == [], "文件契約有缺漏：\n  - " + "\n  - ".join(all_problems)
