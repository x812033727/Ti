"""QA 任務 #3：守護測試——「release 建立後 release-smoke 必定觸發」觸發鏈。

對應驗收標準（破壞性思考，預設東西是壞的直到證明能動）：
  AC#1 publish-release.yml 在 v* tag 出現時執行 `gh release create`，狀態 published
       （不帶 --draft），能被 release:published 接住。
  AC#3 觸發死結解除：以 PAT（secrets.GH_PAT）身分建立 release，而非 GITHUB_TOKEN
       /github.token——後者建立的 release 不觸發下游 workflow（GitHub 防遞迴）。
       且此方案在 workflow 中**實際落地於 step env**（非僅註解）。
  AC#4 守護測試讀真實 yaml，斷言建立 step 存在＋body 來源為 render_tag_notes＋
       觸發鏈成立；反向 mutation（PAT→GITHUB_TOKEN／移除 step／改 trigger／加 --draft）
       必須讓檢查翻紅——附非空 mutation 斷言，杜絕孤立假綠。

設計（為「快樂路徑以外」發聲）：
  - 同一把尺 `check_trigger_chain(publish, smoke)` 給正向與 mutation 共用：
    正向 assert 無問題；mutation 後 assert 該問題確實被捕捉，證明檢查有真鑑別力。
  - 斷言「實際 env 值」而非註解：把 token 來源從 parse 出的 step.env 取，
    避免「註解寫了 PAT、env 卻是 GITHUB_TOKEN」的假綠（AC#3 明指「非僅註解」）。
  - YAML 1.1 會把 `on:` 解析成布林 True key——以 `_trigger_of` 兩路兼容取值，
    不假設 key 字面是字串 'on'。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
PUBLISH_YML = ROOT / ".github" / "workflows" / "publish-release.yml"
SMOKE_YML = ROOT / ".github" / "workflows" / "release-smoke.yml"


# ---------------------------------------------------------------------------
# 解析輔助
# ---------------------------------------------------------------------------


def _trigger_of(doc: dict):
    """取 workflow 的 trigger 區塊。YAML 1.1 把 `on:` 解析成布林 True，故兩路兼容。"""
    if True in doc:
        return doc[True]
    return doc.get("on")


def _steps_of(doc: dict) -> list[dict]:
    jobs = doc.get("jobs", {})
    steps: list[dict] = []
    for job in jobs.values():
        steps.extend(job.get("steps", []) or [])
    return steps


def _find_step_by_run_substr(doc: dict, substr: str) -> dict | None:
    for step in _steps_of(doc):
        if substr in (step.get("run") or ""):
            return step
    return None


# ---------------------------------------------------------------------------
# 核心檢查器：回傳問題清單（空＝觸發鏈完整）。正向與 mutation 共用同一把尺。
# ---------------------------------------------------------------------------


def check_trigger_chain(publish_text: str, smoke_text: str) -> list[str]:
    """讀兩份 workflow 文字，檢查「建立→注入→觸發」三段是否成立。"""
    problems: list[str] = []
    pub = yaml.safe_load(publish_text)
    smoke = yaml.safe_load(smoke_text)

    # --- 段 1：建立 release 的觸發落點是 push tag v* ---
    trig = _trigger_of(pub) or {}
    tags = (trig.get("push") or {}).get("tags") or []
    if not any(str(t).startswith("v") for t in tags):
        problems.append("publish-release 未在 push tag v* 觸發")

    # --- 段 1：Create release step 存在、published（不帶 --draft）---
    create = _find_step_by_run_substr(pub, "gh release create")
    if create is None:
        problems.append("缺 `gh release create` 建立 step")
    else:
        run = create["run"]
        if "--draft" in run:
            problems.append(
                "gh release create 帶 --draft，狀態非 published，無法被 release:published 接住"
            )
        # --- 段 3（觸發死結核心）：以 PAT 身分建立，非 GITHUB_TOKEN/github.token ---
        token = (create.get("env") or {}).get("GH_TOKEN", "")
        if "secrets.GH_PAT" not in token:
            problems.append(f"Create release 的 GH_TOKEN 非 PAT（實際: {token!r}）——觸發死結未解")
        if "github.token" in token.lower() or "github_token" in token.lower():
            problems.append(
                f"Create release 用 GITHUB_TOKEN，建立的 release 不觸發下游（實際: {token!r}）"
            )

    # --- 段 2：body 來源為 render_tag_notes（經 -F body.md 注入、不 shell 拼裝）---
    render = _find_step_by_run_substr(pub, "publish_release.py")
    if render is None:
        problems.append("缺執行 scripts/publish_release.py 的 Render body step")
    if create is not None and "-F body.md" not in create["run"]:
        problems.append("Create release 未以 -F body.md 注入 body（疑似 shell 字串拼裝）")

    # --- 段 3：下游 smoke 以 release:published 接住 ---
    strig = _trigger_of(smoke) or {}
    rtypes = (strig.get("release") or {}).get("types") or []
    if "published" not in rtypes:
        problems.append(f"release-smoke 未以 release:published 觸發（實際 types: {rtypes}）")

    return problems


# ---------------------------------------------------------------------------
# 前提
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def publish_text() -> str:
    assert PUBLISH_YML.exists(), f"AC#1：缺 publish-release.yml {PUBLISH_YML}"
    return PUBLISH_YML.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def smoke_text() -> str:
    assert SMOKE_YML.exists(), f"前提失效：缺 release-smoke.yml {SMOKE_YML}"
    return SMOKE_YML.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 正向：真實 yaml 觸發鏈完整無問題
# ---------------------------------------------------------------------------


def test_trigger_chain_intact_on_real_yaml(publish_text, smoke_text):
    """讀真實 yaml：建立→注入→觸發三段全在，無任何問題。"""
    problems = check_trigger_chain(publish_text, smoke_text)
    assert problems == [], "AC#3/#4：真實 workflow 觸發鏈有破口：\n  - " + "\n  - ".join(problems)


def test_create_release_uses_pat_not_github_token(publish_text):
    """AC#3 直接斷言：Create release 的 GH_TOKEN env 實值來自 secrets.GH_PAT。"""
    pub = yaml.safe_load(publish_text)
    create = _find_step_by_run_substr(pub, "gh release create")
    assert create is not None, "缺 Create release step"
    token = (create.get("env") or {}).get("GH_TOKEN", "")
    assert "secrets.GH_PAT" in token, f"AC#3：Create release 未用 PAT（實值 {token!r}）"
    assert "github.token" not in token.lower(), f"AC#3：仍用 GITHUB_TOKEN（{token!r}）"


def test_create_release_published_not_draft(publish_text):
    """AC#1：release 狀態為 published（不帶 --draft），才能被 release:published 接住。"""
    pub = yaml.safe_load(publish_text)
    create = _find_step_by_run_substr(pub, "gh release create")
    assert create is not None and "--draft" not in create["run"], (
        "AC#1：release 帶 --draft 或缺建立 step"
    )


def test_smoke_triggered_by_release_published(smoke_text):
    """AC#3：下游 smoke 以 release:published 接住建立事件。"""
    smoke = yaml.safe_load(smoke_text)
    rtypes = (_trigger_of(smoke).get("release") or {}).get("types") or []
    assert "published" in rtypes, f"AC#3：smoke 觸發 types 非 published：{rtypes}"


# ---------------------------------------------------------------------------
# 反向 mutation：缺任一段必翻紅（非空斷言，杜絕孤立假綠）
# ---------------------------------------------------------------------------


def test_mutation_pat_to_github_token_turns_red(publish_text, smoke_text):
    """把 PAT 改回 GITHUB_TOKEN → 觸發死結重現 → 檢查必須捕捉。"""
    mutated = publish_text.replace("secrets.GH_PAT", "github.token")
    assert mutated != publish_text, "mutation 無效：未替換到 GH_PAT（孤立假綠風險）"
    problems = check_trigger_chain(mutated, smoke_text)
    assert any("GITHUB_TOKEN" in p or "PAT" in p for p in problems), (
        f"假綠：PAT→GITHUB_TOKEN 後檢查未翻紅，problems={problems}"
    )


def test_mutation_remove_create_step_turns_red(publish_text, smoke_text):
    """移除建立 step（把 `gh release create` 改名）→ 檢查必須捕捉缺 step。"""
    mutated = publish_text.replace("gh release create", "echo skip-create")
    assert mutated != publish_text, "mutation 無效：未替換到建立指令"
    problems = check_trigger_chain(mutated, smoke_text)
    assert any("建立 step" in p for p in problems), (
        f"假綠：移除建立 step 後未翻紅，problems={problems}"
    )


def test_mutation_add_draft_turns_red(publish_text, smoke_text):
    """建立 release 加 --draft → 狀態非 published → 檢查必須捕捉。"""
    mutated = publish_text.replace("-F body.md", "--draft -F body.md")
    assert mutated != publish_text, "mutation 無效：未替換到 create 指令"
    problems = check_trigger_chain(mutated, smoke_text)
    assert any("--draft" in p for p in problems), f"假綠：加 --draft 後未翻紅，problems={problems}"


def test_mutation_smoke_published_to_created_turns_red(publish_text, smoke_text):
    """smoke trigger 由 published 改 created → draft 不觸發 → 檢查必須捕捉。"""
    mutated_smoke = smoke_text.replace("[published]", "[created]")
    assert mutated_smoke != smoke_text, "mutation 無效：未替換到 trigger types"
    problems = check_trigger_chain(publish_text, mutated_smoke)
    assert any("release:published" in p for p in problems), (
        f"假綠：smoke 改 created 後未翻紅，problems={problems}"
    )


def test_mutation_remove_render_step_turns_red(publish_text, smoke_text):
    """移除 render body step（body 不再來自 render_tag_notes）→ 檢查必須捕捉。"""
    mutated = publish_text.replace("publish_release.py", "noop.py")
    assert mutated != publish_text, "mutation 無效：未替換到 render 指令"
    problems = check_trigger_chain(mutated, smoke_text)
    assert any("Render body step" in p for p in problems), (
        f"假綠：移除 render step 後未翻紅，problems={problems}"
    )


# ---------------------------------------------------------------------------
# 任務 #3：PAT guard step 攔「GH_PAT secret 未設」
#   觸發死結的根因之一是「忘了設 GH_PAT」——guard 把它攔在第一步，CI log 直指根因，
#   而非到 Create release 才以 403 撞權限。guard 特徵：env.GH_TOKEN 來自 secrets.GH_PAT、
#   run 內以 `test -n "$GH_TOKEN"` 偵測空字串並非零退出，且本身不是建立 step。
# ---------------------------------------------------------------------------


def _find_pat_guard_step(doc: dict) -> dict | None:
    """找「PAT 未設」guard step：用 test -n 檢查 GH_TOKEN 且非建立 release 的 step。"""
    for step in _steps_of(doc):
        run = step.get("run") or ""
        if "gh release create" in run:
            continue  # 建立 step 不算 guard
        if 'test -n "$GH_TOKEN"' in run:
            return step
    return None


def check_pat_guard(publish_text: str) -> list[str]:
    """檢查 PAT guard：存在、攔空、非零退出、env 來源為 secrets.GH_PAT。空＝完整。"""
    problems: list[str] = []
    pub = yaml.safe_load(publish_text)
    guard = _find_pat_guard_step(pub)
    if guard is None:
        problems.append('缺 PAT guard step（無 `test -n "$GH_TOKEN"` 攔截）')
        return problems
    run = guard.get("run") or ""
    if "exit 1" not in run:
        problems.append("PAT guard 偵測到未設卻未非零退出（缺 exit 1，無法 fail-fast）")
    token = (guard.get("env") or {}).get("GH_TOKEN", "")
    if "secrets.GH_PAT" not in token:
        problems.append(f"PAT guard 檢查的不是 GH_PAT secret（實際: {token!r}）")
    return problems


def test_pat_guard_intact_on_real_yaml(publish_text):
    """任務 #3：真實 yaml 的 PAT guard 完整——存在、攔空、非零退出、查的是 GH_PAT。"""
    problems = check_pat_guard(publish_text)
    assert problems == [], "PAT guard 有破口：\n  - " + "\n  - ".join(problems)


def test_mutation_remove_pat_guard_turns_red(publish_text):
    """移除 guard 的偵測邏輯（test -n 改掉）→ 檢查必須翻紅。"""
    mutated = publish_text.replace('test -n "$GH_TOKEN"', "true")
    assert mutated != publish_text, "mutation 無效：未替換到 guard 偵測（孤立假綠風險）"
    problems = check_pat_guard(mutated)
    assert any("guard" in p for p in problems), f"假綠：移除 guard 後未翻紅，problems={problems}"


def test_mutation_guard_drops_exit_turns_red(publish_text):
    """guard 偵測到未設卻不 exit（移除 exit 1）→ 形同空轉 → 檢查必須翻紅。"""
    guard = _find_pat_guard_step(yaml.safe_load(publish_text))
    assert guard is not None, "前提失效：找不到 guard step"
    mutated = publish_text.replace(guard["run"], guard["run"].replace("exit 1", "true"))
    assert mutated != publish_text, "mutation 無效：未替換到 exit 1"
    problems = check_pat_guard(mutated)
    assert any("非零退出" in p for p in problems), (
        f"假綠：guard 去掉 exit 1 後未翻紅，problems={problems}"
    )


# ---------------------------------------------------------------------------
# AC#2：YAML 與腳本內零 Breaking heading 字面值（0 命中）
# ---------------------------------------------------------------------------


def test_no_breaking_heading_literal_in_yaml_and_script(publish_text, smoke_text):
    """heading 字面值（## ⚠️ Breaking Changes / ## Breaking）不得硬寫於 yaml/腳本。"""
    from studio.release_note import BREAKING_HEADING

    script_text = (ROOT / "scripts" / "publish_release.py").read_text(encoding="utf-8")
    for name, text in (
        ("publish.yml", publish_text),
        ("smoke.yml", smoke_text),
        ("publish_release.py", script_text),
    ):
        assert BREAKING_HEADING not in text, (
            f"AC#2：{name} 出現 heading 字面值 {BREAKING_HEADING!r}"
        )
        assert "## Breaking" not in text, f"AC#2：{name} 出現 `## Breaking` 字面值"
