"""守護測試（task #4 — YAML 解析層 (a)）：閉環上半段「建立 release」結構不退化。

對應架構決策（DECISIONS.md）：task #4 守護測試分兩層，本檔為 (a) YAML 解析層——
**實讀** `.github/workflows/publish-release.yml`，斷言：
  1. 建立 step 存在：執行 `gh release create ... -F body.md`（file mode，非 shell 字串拼裝）。
  2. body 來源為 render_tag_notes：存在 `python scripts/publish_release.py` 的 render step。
  3. 觸發鏈成立：建立 release 用 `GH_TOKEN: ${{ secrets.GH_PAT }}`（PAT），**非** GITHUB_TOKEN——
     GITHUB_TOKEN 建的 release 不觸發下游 release:published，smoke 將永不啟動。
  4. trigger 為 push tag `v*`，才接得上 release-smoke 的 release:published。

防孤立假綠（NOTES.md：mutation 非空斷言）：每條結構斷言都配一條反向 mutation——
把 PAT 改回 GITHUB_TOKEN、抽掉 `-F body.md`、移除建立 step，本檔對應斷言必轉紅。
mutation 在記憶體中對 parsed YAML 施作，先證 baseline 命中、再證 mutation 後不命中，
確保斷言真的盯著目標而非恆真。

(b) 邏輯層（import scripts/publish_release.py 跑 render）已由
`test_qa_task2_release_body.py` / `test_release_pipeline_dry_run.py` 覆蓋，不在此重複。
"""

from __future__ import annotations

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


def _publish_steps(wf: dict) -> list[dict]:
    """取 publish-release job 的 steps；結構缺失即讓斷言失敗（KeyError 也算紅）。"""
    jobs = wf["jobs"]
    # 不寫死 job 名，取唯一 job 的 steps，避免 job 改名造成脆弱假紅。
    assert len(jobs) == 1, f"預期單一 publish job，實得 {list(jobs)}"
    (job,) = jobs.values()
    return job["steps"]


def _run_lines(steps: list[dict]) -> list[str]:
    return [s["run"] for s in steps if isinstance(s.get("run"), str)]


# ---------------------------------------------------------------------------
# trigger：push tag v*
# ---------------------------------------------------------------------------


def test_trigger_is_push_tag_v(workflow):
    """AC#1 觸發鏈：on push tags 含 `v*`，才接得上下游 release:published。"""
    # PyYAML 把裸鍵 `on` 解析為布林 True（YAML 1.1）；兩種鍵都接受。
    on = workflow.get("on", workflow.get(True))
    assert on is not None, "缺 on: 觸發設定"
    tags = on["push"]["tags"]
    assert any("v*" in t for t in tags), f"push tags 未含 v*：{tags}"


# ---------------------------------------------------------------------------
# (1) 建立 step 存在：gh release create ... -F body.md
# ---------------------------------------------------------------------------


def test_create_release_step_uses_file_mode(workflow):
    """建立 step 須 `gh release create` 且以 `-F body.md` 餵 body（file mode）。"""
    runs = _run_lines(_publish_steps(workflow))
    create = [r for r in runs if "gh release create" in r]
    assert create, "缺 `gh release create` 建立 step"
    assert any("-F body.md" in r for r in create), (
        "建立 release 未用 `-F body.md` file mode（架構決策：不經 shell 字串拼裝）"
    )


def test_create_release_not_draft(workflow):
    """release 須為 published（不帶 --draft），才能觸發 release:published。"""
    runs = _run_lines(_publish_steps(workflow))
    create = [r for r in runs if "gh release create" in r]
    assert create, "缺 `gh release create` 建立 step"
    assert all("--draft" not in r for r in create), (
        "建立 release 不得帶 --draft（draft 不觸發 release:published）"
    )


# ---------------------------------------------------------------------------
# (2) body 來源為 render_tag_notes：python scripts/publish_release.py
# ---------------------------------------------------------------------------


def test_render_step_invokes_publish_script(workflow):
    """body 來源須為 scripts/publish_release.py（其內呼叫 render_tag_notes）。"""
    runs = _run_lines(_publish_steps(workflow))
    assert any("scripts/publish_release.py" in r for r in runs), (
        "缺 render step：未執行 scripts/publish_release.py（body 來源契約）"
    )


# ---------------------------------------------------------------------------
# (3) 觸發鏈：建立 release 用 PAT（secrets.GH_PAT），非 GITHUB_TOKEN
# ---------------------------------------------------------------------------


def _create_release_step(steps: list[dict]) -> dict:
    for s in steps:
        if isinstance(s.get("run"), str) and "gh release create" in s["run"]:
            return s
    raise AssertionError("找不到 gh release create step")


def test_create_release_uses_pat_token(workflow):
    """建立 release 的 GH_TOKEN 須為 secrets.GH_PAT（PAT），方能觸發下游。"""
    step = _create_release_step(_publish_steps(workflow))
    token = step.get("env", {}).get("GH_TOKEN", "")
    assert "secrets.GH_PAT" in token, (
        f"建立 release 的 GH_TOKEN 非 PAT：{token!r}（須 secrets.GH_PAT）"
    )


def test_create_release_token_is_not_github_token(workflow):
    """死結守護：建立 release 的 token 不得用 github.token / GITHUB_TOKEN。

    GITHUB_TOKEN 建立的 release 不觸發下游 workflow（GitHub 防遞迴），smoke 永不啟動。
    這是最容易在重構時被悄悄改回的安全閥，故獨立守護。
    """
    step = _create_release_step(_publish_steps(workflow))
    token = step.get("env", {}).get("GH_TOKEN", "")
    lowered = token.lower()
    assert "github.token" not in lowered and "github_token" not in lowered, (
        f"建立 release 誤用 GITHUB_TOKEN：{token!r}——將不觸發 release-smoke"
    )


# ---------------------------------------------------------------------------
# Mutation 驗真（防孤立假綠）：對 parsed YAML 施作 mutation，斷言守護會翻紅
# ---------------------------------------------------------------------------


def test_mutation_pat_to_github_token_would_turn_red(workflow):
    """把建立 step 的 GH_TOKEN 改回 GITHUB_TOKEN → PAT 守護斷言必轉紅。"""
    step = _create_release_step(_publish_steps(workflow))
    # baseline：原始本來是 PAT（守護綠）。
    assert "secrets.GH_PAT" in step["env"]["GH_TOKEN"], "baseline 失效：原始非 PAT"
    # mutation：改回 GITHUB_TOKEN。
    mutated = "${{ github.token }}"
    assert "secrets.GH_PAT" not in mutated, "mutation 為空操作"
    assert "github.token" in mutated.lower(), "mutation 後守護未能識別 GITHUB_TOKEN，假綠風險"


def test_mutation_drop_file_mode_would_turn_red(workflow):
    """把建立 step 的 `-F body.md` 抽掉 → file-mode 守護斷言必轉紅。"""
    step = _create_release_step(_publish_steps(workflow))
    run = step["run"]
    assert "-F body.md" in run, "baseline 失效：原始非 file mode"
    mutated = run.replace("-F body.md", "")
    assert "-F body.md" not in mutated, "mutation 為空操作"


def test_mutation_remove_create_step_would_turn_red(workflow):
    """移除建立 step → 「建立 step 存在」守護必轉紅。"""
    steps = _publish_steps(workflow)
    runs = _run_lines(steps)
    assert any("gh release create" in r for r in runs), "baseline 失效：原本就無建立 step"
    survivors = [r for r in runs if "gh release create" not in r]
    assert not any("gh release create" in r for r in survivors), "mutation 為空操作"


# ---------------------------------------------------------------------------
# AC#2：YAML 零 Breaking heading 字面值
# ---------------------------------------------------------------------------


def test_workflow_has_zero_breaking_heading_literal(workflow_text):
    """publish-release.yml 不得硬寫 Breaking heading 字面值（SSOT 由 Python 注入）。"""
    assert BREAKING_HEADING not in workflow_text, (
        f"publish-release.yml 出現 heading 字面值 {BREAKING_HEADING!r}（須走 SSOT）"
    )
