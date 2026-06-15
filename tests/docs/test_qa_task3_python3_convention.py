"""QA 驗收：任務 #3「文件化 `python3` 慣例與 Windows 退路」一致性測試。

任務 #3 屬文件化，無可執行邏輯；本測試以「文件一致性檢查」釘住成果，避免日後文件腐化
而「`python3` 找不到 → `command not found`」痛點復發。守護兩條新需求：

- (A) README 與 CONTRIBUTING 必須文件化 **Windows `py` 退路**：
  當系統裝了 Python 但 `python3` 不在 PATH 時，Windows 用 `py` 啟動器是官方推薦的退路。

- (B) README 或 CONTRIBUTING 必須文件化 **`python3` 慣例**：
  慣例兩面：(1) venv 內允許用 `python`（執行檔名）；(2) shell 範例統一 `python3`。
  兩面都要**明確聲明**才算守護住——只在敘述中順手提到不算。

判定策略：抓「獨立 `py` token」+ 上下文關鍵詞，避免子字串偽綠（如 `pyproject`、`python3-*`）。
所有斷言在 pytest 失敗時印出「該寫什麼範例句」提示，方便工程師一次到位。
"""

from __future__ import annotations

import re

import pytest

from _repo import REPO_ROOT

_ROOT = REPO_ROOT
README = (_ROOT / "README.md").read_text(encoding="utf-8")
CONTRIB = (_ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")


# ============================================================================
# 共用判定：避免子字串偽綠（`pyproject` / `python3-pip` / `python_version` 等）
# ============================================================================


def _has_independent_py_token(text: str) -> bool:
    """抓「`py`」獨立 token（含 backtick）——前後非 word 字元。"""
    return bool(re.search(r"(?<![\w/.-])`?py`?(?![\w.-])", text))


def _has_windows_py_launcher_hint(text: str) -> bool:
    """判斷文本是否在 Windows 語境下說明 `py` 是退路。

    條件（AND）：
      1. 檔案內出現獨立 `py` token；
      2. 同檔出現「找不到」「改用」「fallback」「退路」之一；
      3. 「py」與「找不到/改用/退路/fallback」**相距 ≤ 80 字**（同一語意單元）。

    此判定避免「pyproject」/「python3-pip」/「TI_IMPROVE_MAX_CYCLES 找不到新改善點」
    等子字串偽綠；只接受「Windows 找不到 python3 → 改用 `py`」這條語意鏈。
    """
    if not _has_independent_py_token(text):
        return False
    fallback_signals = r"(找不到|改用|fallback|退路|launcher|啟動器)"
    if not re.search(fallback_signals, text):
        return False
    # 同語意單元：「py」後 80 字內有 fallback 訊號（涵蓋「`py` 啟動器」/「`py -3`」/
    # 「改用 `py`」/「`py` 找不到」等句型；順序不限以涵蓋「找不到時改用 `py`」）。
    pair_pat = re.compile(
        r"`?py`?[^\n]{0,80}(" + fallback_signals + r")"
        r"|(" + fallback_signals + r")[^\n`]{0,80}`?py`?",
    )
    return bool(pair_pat.search(text))


def _has_venv_python_explicit(text: str) -> bool:
    """判斷文本是否**明確聲明** venv 內允許 `python`（慣例敘述，非順手帶過）。

    條件（AND）：
      1. 提到 venv（任一處）；
      2. 該處 60 字內出現「允許」「使用」「用」「走」+ `python` 任一形式。

    排除「`.venv/bin/python3` 完整路徑」這類純寫法描述。
    """
    if "venv" not in text:
        return False
    pat = re.compile(
        r"venv[^\n]{0,60}(允許|使用|用|走|沿用|可)[^\n]{0,30}`?python`?",
    )
    return bool(pat.search(text))


def _has_shell_python3_explicit_convention(text: str) -> bool:
    """判斷文本是否**明確聲明** shell 範例統一用 `python3`（慣例敘述）。

    條件（AND）：同檔內同時出現
      1. 「shell」「範例」「文件」「demo」「指令」「命令」其一；
      2. 「統一」「慣例」「收斂」「canonical」「convention」其一；
      3. `python3` 字串。
      且三者在 100 字內構成同一語意單元。
    """
    pat = re.compile(
        r"(shell|範例|文件|demo|指令|命令)[^\n]{0,40}"
        r"(統一|慣例|收斂|canonical|convention)[^\n`]{0,40}`?python3`?"
        r"|"
        r"(統一|慣例|收斂|canonical|convention)[^\n]{0,40}"
        r"(shell|範例|文件|demo|指令|命令)[^\n`]{0,40}`?python3`?",
    )
    return bool(pat.search(text))


# ============================================================================
# (A) Windows `py` 退路說明：README 與 CONTRIBUTING 都要有
# ============================================================================


def test_readme_documents_windows_py_fallback():
    """README 必須文件化 Windows 的 `py` 退路說明（明確語意鏈）。"""
    assert _has_windows_py_launcher_hint(README), (
        "README 缺 Windows `py` 退路說明（`py` 與『找不到/改用』未構成同語意單元）。\n"
        "需在『執行環境前置』或 happy-path 段加一句具體示範，範例句：\n"
        "  > Windows 若 `python3` 找不到，可改用 `py` 啟動器（想鎖 3.x 用 `py -3`）。"
    )


def test_contributing_documents_windows_py_fallback():
    """CONTRIBUTING 必須文件化 Windows 的 `py` 退路說明（對內對外一致）。"""
    assert _has_windows_py_launcher_hint(CONTRIB), (
        "CONTRIBUTING 缺 Windows `py` 退路說明。需在『環境建置』或新慣例節補上。\n"
        "範例句：> Windows 若 `python3` 找不到，可改用 `py` 啟動器"
        "（想鎖 3.x 用 `py -3`）；這是 Python 官方在 Windows 推薦的退路。"
    )


# ============================================================================
# (B) `python3` 慣例：venv 內允許 `python`、shell 範例統一 `python3`
# ============================================================================


def test_readme_or_contributing_documents_venv_python_allowed():
    """README 或 CONTRIBUTING 必須**明確聲明** venv 內允許 `python`（不是順手帶過）。"""
    assert _has_venv_python_explicit(README) or _has_venv_python_explicit(CONTRIB), (
        "兩份文件皆未**明確聲明**『venv 內允許使用 `python`』慣例。\n"
        "需在 CONTRIBUTING『環境建置』段或 README 補一句話，範例句：\n"
        "  > venv 內執行腳本允許使用 `python`"
        "（mac/Linux 為 `.venv/bin/python`、Windows 為 `.venv\\Scripts\\python`），"
        "慣例由 .venv 完整路徑自然收斂。"
    )


def test_readme_or_contributing_documents_shell_python3_unified():
    """README 或 CONTRIBUTING 必須**明確聲明** shell 範例統一 `python3`（不是順手帶過）。"""
    assert _has_shell_python3_explicit_convention(README) or _has_shell_python3_explicit_convention(
        CONTRIB
    ), (
        "兩份文件皆未**明確聲明**『shell 範例統一 `python3`』慣例（純寫法描述不算）。\n"
        "需在 CONTRIBUTING 或 README 補一句話，範例句：\n"
        "  > 本專案 shell 範例（文件 demo、shell script 範例）統一使用 `python3`；"
        "venv 內執行檔路徑（`.venv/bin/python` / `.venv\\Scripts\\python`）與套件名、"
        "image tag 維持原樣。"
    )


# ============================================================================
# (C) README 退路具體可重現：必須有可被讀者照著用的具體 `py` 指令字串
# ============================================================================


def test_readme_py_fallback_specific_phrase():
    """README 退路說明必須有具體可照用的 `py` 指令字串（非抽象形容）。"""
    has_specific = bool(
        re.search(
            r"`py`(\s*啟動器|\s*-3\b|執行|跑|app\b|main\b)|"
            r"`py -3`|`py`\s*app|`py`\s*main",
            README,
        )
    )
    assert has_specific, (
        "README 退路說明缺具體可照用的字串。需在『執行環境前置』或 happy-path 段"
        "加一句具體示範，例如：\n"
        "  > Windows 若 `python3` 找不到，可改用 `py` 啟動器"
        "（想鎖 3.x 用 `py -3`）。"
    )


# ============================================================================
# (D) 負樣本（防假綠）：制度化「regex 類守護測試須含 ≥1 個負樣斷言」
# ============================================================================
# 依 CONTRIBUTING「Python interpreter convention」節之規範：tests/docs 中
# regex 類守護測試須含 ≥1 個負樣斷言，否則視為假綠。本節用 3 個典型偽綠
# （套件名、env var 子字串、pyproject 子字串）守住 `_has_*` 函式的字邊界
# 與前後瞻邏輯——這些輸入理應不被誤判為「文件化 Python 慣例」。


@pytest.mark.parametrize(
    "neg_text,why",
    [
        ("請安裝 python3-pip 套件以獲得 pip 支援。", "套件名子字串偽綠"),
        ("TI_IMPROVE_MAX_CYCLES 找不到新改善點", "env var 子字串『找不到』偽綠"),
        ("請參考 pyproject.toml 的設定", "pyproject 子字串偽綠"),
    ],
)
def test_negative_samples_must_not_trigger_convention_matchers(neg_text, why):
    """負樣本：套件名／env var／pyproject 等子字串不該誤觸慣例判斷。"""
    py_hint = _has_windows_py_launcher_hint(neg_text)
    venv = _has_venv_python_explicit(neg_text)
    shell = _has_shell_python3_explicit_convention(neg_text)
    assert not (py_hint or venv or shell), (
        f"負樣本『{why}』誤觸：py_hint={py_hint} venv={venv} shell={shell}\n"
        f"  輸入: {neg_text!r}"
    )
