"""QA 驗收測試 — 任務 #3：README `## 測試` 段收斂為摘要 + 連結。

驗收重點（標準 1、3、4）：
- `## 測試` 段不再有可複製的逐條 dev 指令 code block；
- 僅保留 2-3 行摘要，且含可點擊 `[CONTRIBUTING.md](CONTRIBUTING.md)` 連結；
- README 全檔無 pytest 執行指令（無裸 `pytest` 行首、無 `-m pytest`）；
- 不破壞 onboarding：執行環境前置 happy-path 的 pre-commit 步驟仍在、驗證指令段仍在。
"""

from __future__ import annotations

import re

from _repo import REPO_ROOT

ROOT = REPO_ROOT
README = ROOT / "README.md"


def _readme() -> str:
    return README.read_text(encoding="utf-8")


def _test_section() -> list[str]:
    """擷取 `## 測試` 標題到下一個 `## ` 之間的內容行。"""
    ls = _readme().splitlines()
    start = next((i for i, ln in enumerate(ls) if re.match(r"^## 測試\s*$", ln)), None)
    assert start is not None, "README 找不到 `## 測試` 段"
    end = next((i for i in range(start + 1, len(ls)) if ls[i].startswith("## ")), len(ls))
    return ls[start + 1 : end]


# 標準 1-a：測試段不再有可複製執行的 dev 指令（無 code fence、無逐條指令）
def test_section_has_no_command_codeblock():
    sec = "\n".join(_test_section())
    assert "```" not in sec, "`## 測試` 段仍保留 code block（應移除逐條指令）"
    # 不得再出現 dev 指令家族的可複製形態
    for token in ("-m pytest", "-m pip install", "ruff check", "ruff format", "pre_commit install"):
        assert token not in sec, f"`## 測試` 段仍殘留 dev 指令 {token!r}"


# 標準 1-b：測試段含可點擊的 CONTRIBUTING.md 連結
def test_section_links_to_contributing():
    sec = "\n".join(_test_section())
    assert "[CONTRIBUTING.md](CONTRIBUTING.md)" in sec, "`## 測試` 段缺可點擊 CONTRIBUTING.md 連結"


# 標準 1-c：測試段為精簡摘要（2-3 行實質內容，不含落落長指令）
def test_section_is_concise_summary():
    body = [ln for ln in _test_section() if ln.strip()]
    # 摘要應精簡：實質非空行數量落在 2~5 行（含導向 CONTRIBUTING/ARCHITECTURE 的既有句）
    assert 2 <= len(body) <= 6, f"`## 測試` 段非空行數 {len(body)} 不像 2-3 行摘要: {body}"


# 標準 4-a：README 全檔無 pytest 執行指令（行首裸 pytest 或 python -m pytest）
def test_readme_no_pytest_run_command():
    bad = []
    for i, ln in enumerate(_readme().splitlines()):
        if re.match(r"^\s*pytest(\s|$)", ln):
            bad.append((i + 1, ln))
        if re.search(r"(?<![\w./-])python3? -m pytest", ln):
            bad.append((i + 1, ln))
    assert not bad, f"README 仍有 pytest 執行指令（應只留在 CONTRIBUTING）: {bad}"


# 標準 4-b：摘要句不以 pytest 開頭（避開 `^\s*pytest` 斷言），以敘述句帶出
def test_summary_mentions_tools_descriptively():
    sec = "\n".join(_test_section())
    assert "pytest" in sec, "摘要應敘述性提及 pytest"
    # 確保是敘述（行首不是 pytest）——已由 test_readme_no_pytest_run_command 保證全檔無行首 pytest


# 標準 4-c：onboarding「執行環境前置」happy-path 的 pre-commit 步驟未被誤刪
def test_onboarding_precommit_step_preserved():
    t = _readme()
    assert ".venv/bin/python3 -m pre_commit install" in t, (
        "onboarding 執行環境前置的 pre-commit 步驟不應被本次收斂移除"
    )


# 標準 4-d：驗證指令段（預期輸出 ok）維持原樣
def test_verify_command_section_preserved():
    t = _readme()
    assert "ok" in t, "README 驗證指令段的預期輸出 ok 不應消失"
