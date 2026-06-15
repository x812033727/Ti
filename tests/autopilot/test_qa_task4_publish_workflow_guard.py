"""守護測試（task #4 — YAML 解析層 (a)）：閉環上半段「建立 release」結構不退化。

對應架構決策（DECISIONS.md）：task #4 守護測試分兩層，本檔為 (a) YAML 解析層——
**實讀** `.github/workflows/publish-release.yml`，斷言：
  1. 建立 step 存在：執行 `gh release create ... -F body.md`（file mode，非 shell 字串拼裝）。
  2. body 來源為 render_tag_notes：存在 `python scripts/publish_release.py` 的 render step。
  3. 觸發鏈成立：建立 release 用 `GH_TOKEN: ${{ secrets.GH_PAT }}`（PAT），**非** GITHUB_TOKEN——
     GITHUB_TOKEN 建的 release 不觸發下游 release:published，smoke 將永不啟動。
  4. trigger 為 push tag `v*`，才接得上 release-smoke 的 release:published。

防孤立假綠（NOTES.md：mutation 非空斷言）：每條結構斷言都抽成**可重用判斷式**
（`uses_pat`、`is_github_token`、`runs_have_file_mode`…），baseline 與 mutation 共用同一式。
mutation 流程＝深拷貝 parsed YAML → 在記憶體中破壞目標 → **把破壞後的結構餵回同一判斷式**，
斷言 baseline 命中、mutation 後不命中。如此守護是否真有鑑別力、由判斷式本身證明，
而非斷言「字串 X 含 X」這類同義反覆（後者即便守護邏輯壞掉也恆綠＝假綠）。

(b) 邏輯層（import scripts/publish_release.py 跑 render）已由
`test_qa_task2_release_body.py` / `test_release_pipeline_dry_run.py` 覆蓋，不在此重複。
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml

# 零 Breaking heading 字面值：heading 契約一律由 SSOT 常數承載，本檔不硬寫字面值。
from studio.release_note import BREAKING_HEADING

_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_PATH = _ROOT / ".github" / "workflows" / "publish-release.yml"


@pytest.fixture(scope="module")
def workflow_text() -> str:
    assert WORKFLOW_PATH.exists(), f"前提失效：缺 publish-release.yml {WORKFLOW_PATH}"
    return WORKFLOW_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def workflow(workflow_text) -> dict:
    return yaml.safe_load(workflow_text)


@pytest.fixture
def wf_copy(workflow) -> dict:
    """每個 mutation 測試拿一份深拷貝，破壞它不污染 module-scope 的真實 parse 結果。"""
    return copy.deepcopy(workflow)


# ---------------------------------------------------------------------------
# 結構萃取
# ---------------------------------------------------------------------------


def _publish_steps(wf: dict) -> list[dict]:
    """取 publish-release job 的 steps；結構缺失即讓斷言失敗（KeyError 也算紅）。"""
    jobs = wf["jobs"]
    # 不寫死 job 名，取唯一 job 的 steps，避免 job 改名造成脆弱假紅。
    assert len(jobs) == 1, f"預期單一 publish job，實得 {list(jobs)}"
    (job,) = jobs.values()
    return job["steps"]


def _run_lines(steps: list[dict]) -> list[str]:
    return [s["run"] for s in steps if isinstance(s.get("run"), str)]


def _create_release_step(steps: list[dict]) -> dict | None:
    for s in steps:
        if isinstance(s.get("run"), str) and "gh release create" in s["run"]:
            return s
    return None


# ---------------------------------------------------------------------------
# 可重用守護判斷式（baseline 與 mutation 共用同一份——這是「mutation 非空」的關鍵：
# 真正被反向測試的是這些函式的鑑別力，而非手寫字串的同義反覆）
# ---------------------------------------------------------------------------


def has_create_step(wf: dict) -> bool:
    """建立 step 存在：有一條 run 含 `gh release create`。"""
    return any("gh release create" in r for r in _run_lines(_publish_steps(wf)))


def has_render_step(wf: dict) -> bool:
    """body 來源契約：有一條 run 執行 scripts/publish_release.py。"""
    return any("scripts/publish_release.py" in r for r in _run_lines(_publish_steps(wf)))


def create_uses_file_mode(wf: dict) -> bool:
    """建立 release 以 `-F body.md` 餵 body（file mode，非 shell 字串拼裝）。"""
    step = _create_release_step(_publish_steps(wf))
    return step is not None and "-F body.md" in step["run"]


def create_is_not_draft(wf: dict) -> bool:
    """建立 release 不帶 --draft（draft 不觸發 release:published）。"""
    step = _create_release_step(_publish_steps(wf))
    return step is not None and "--draft" not in step["run"]


def create_uses_pat(wf: dict) -> bool:
    """建立 release 的 GH_TOKEN 為 secrets.GH_PAT（PAT）。"""
    step = _create_release_step(_publish_steps(wf))
    if step is None:
        return False
    return "secrets.GH_PAT" in step.get("env", {}).get("GH_TOKEN", "")


def create_uses_github_token(wf: dict) -> bool:
    """死結偵測：建立 release 誤用 github.token / GITHUB_TOKEN（不觸發下游）。"""
    step = _create_release_step(_publish_steps(wf))
    if step is None:
        return False
    token = step.get("env", {}).get("GH_TOKEN", "").lower()
    return "github.token" in token or "github_token" in token


def trigger_has_v_tag(wf: dict) -> bool:
    """on push tags 含 `v*`，才接得上下游 release:published。"""
    # PyYAML 把裸鍵 `on` 解析為布林 True（YAML 1.1）；兩種鍵都接受。
    on = wf.get("on", wf.get(True))
    if not on:
        return False
    tags = on.get("push", {}).get("tags", [])
    return any("v*" in t for t in tags)


# ---------------------------------------------------------------------------
# Baseline：真實 YAML 經判斷式檢查皆綠
# ---------------------------------------------------------------------------


def test_trigger_is_push_tag_v(workflow):
    """AC#1 觸發鏈：on push tags 含 `v*`。"""
    assert trigger_has_v_tag(workflow), "push tags 未含 v*"


def test_create_release_step_uses_file_mode(workflow):
    assert has_create_step(workflow), "缺 `gh release create` 建立 step"
    assert create_uses_file_mode(workflow), (
        "建立 release 未用 `-F body.md` file mode（架構決策：不經 shell 字串拼裝）"
    )


def test_create_release_not_draft(workflow):
    assert create_is_not_draft(workflow), (
        "建立 release 不得帶 --draft（draft 不觸發 release:published）"
    )


def test_render_step_invokes_publish_script(workflow):
    assert has_render_step(workflow), (
        "缺 render step：未執行 scripts/publish_release.py（body 來源契約）"
    )


def test_create_release_uses_pat_token(workflow):
    assert create_uses_pat(workflow), "建立 release 的 GH_TOKEN 非 PAT（須 secrets.GH_PAT）"


def test_create_release_token_is_not_github_token(workflow):
    """死結守護：建立 release 的 token 不得用 github.token / GITHUB_TOKEN。

    GITHUB_TOKEN 建立的 release 不觸發下游 workflow（GitHub 防遞迴），smoke 永不啟動。
    這是最容易在重構時被悄悄改回的安全閥，故獨立守護。
    """
    assert not create_uses_github_token(workflow), (
        "建立 release 誤用 GITHUB_TOKEN——將不觸發 release-smoke"
    )


# ---------------------------------------------------------------------------
# Mutation 驗真（防孤立假綠）：深拷貝 → 破壞結構 → 餵回同一判斷式 → 斷言翻紅
# 每條都先證 baseline 命中，再證 mutation 後不命中（mutation 非空）。
# ---------------------------------------------------------------------------


def test_mutation_pat_to_github_token_turns_red(wf_copy):
    """把建立 step 的 GH_TOKEN 改回 GITHUB_TOKEN → PAT 守護翻紅、死結守護觸發。"""
    assert create_uses_pat(wf_copy), "baseline 失效：原始非 PAT"
    assert not create_uses_github_token(wf_copy), "baseline 失效：原始已是 GITHUB_TOKEN"

    step = _create_release_step(_publish_steps(wf_copy))
    step["env"]["GH_TOKEN"] = "${{ github.token }}"  # mutation

    assert not create_uses_pat(wf_copy), "mutation 後 PAT 守護仍綠＝假綠"
    assert create_uses_github_token(wf_copy), "mutation 後死結守護未識別 GITHUB_TOKEN＝假綠"


def test_mutation_drop_file_mode_turns_red(wf_copy):
    """把建立 step 的 `-F body.md` 抽掉 → file-mode 守護翻紅。"""
    assert create_uses_file_mode(wf_copy), "baseline 失效：原始非 file mode"

    step = _create_release_step(_publish_steps(wf_copy))
    step["run"] = step["run"].replace("-F body.md", "")  # mutation

    assert not create_uses_file_mode(wf_copy), "mutation 後 file-mode 守護仍綠＝假綠"


def test_mutation_remove_create_step_turns_red(wf_copy):
    """移除建立 step → 「建立 step 存在」守護翻紅。"""
    assert has_create_step(wf_copy), "baseline 失效：原本就無建立 step"

    steps = _publish_steps(wf_copy)
    steps[:] = [
        s for s in steps if not (isinstance(s.get("run"), str) and "gh release create" in s["run"])
    ]

    assert not has_create_step(wf_copy), "mutation 後建立-step 守護仍綠＝假綠"


def test_mutation_remove_render_step_turns_red(wf_copy):
    """移除 render step → 「body 來源」守護翻紅。"""
    assert has_render_step(wf_copy), "baseline 失效：原本就無 render step"

    steps = _publish_steps(wf_copy)
    steps[:] = [
        s
        for s in steps
        if not (isinstance(s.get("run"), str) and "scripts/publish_release.py" in s["run"])
    ]

    assert not has_render_step(wf_copy), "mutation 後 render-step 守護仍綠＝假綠"


def test_mutation_add_draft_flag_turns_red(wf_copy):
    """建立 step 加上 --draft → not-draft 守護翻紅（draft 不觸發下游）。"""
    assert create_is_not_draft(wf_copy), "baseline 失效：原始已帶 --draft"

    step = _create_release_step(_publish_steps(wf_copy))
    step["run"] = step["run"].replace("gh release create", "gh release create --draft")  # mutation

    assert not create_is_not_draft(wf_copy), "mutation 後 not-draft 守護仍綠＝假綠"


def test_mutation_drop_v_tag_trigger_turns_red(wf_copy):
    """把 push tag `v*` 觸發抽掉 → 觸發鏈守護翻紅。"""
    assert trigger_has_v_tag(wf_copy), "baseline 失效：原本就無 v* 觸發"

    on = wf_copy.get("on", wf_copy.get(True))
    on["push"]["tags"] = ["release-*"]  # mutation：改成接不上 release:published 的 tag

    assert not trigger_has_v_tag(wf_copy), "mutation 後觸發鏈守護仍綠＝假綠"


# ---------------------------------------------------------------------------
# AC#2：YAML 零 Breaking heading 字面值
# ---------------------------------------------------------------------------


def test_workflow_has_zero_breaking_heading_literal(workflow_text):
    """publish-release.yml 不得硬寫 Breaking heading 字面值（SSOT 由 Python 注入）。"""
    assert BREAKING_HEADING not in workflow_text, (
        f"publish-release.yml 出現 heading 字面值 {BREAKING_HEADING!r}（須走 SSOT）"
    )
