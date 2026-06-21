"""Guard：退避秒數計算與 429／529 錯誤文字分類器的唯一實作必須留在 studio/llm_caller.py。

鎖 KNOWN_LIMITATIONS 最後一項與 docs/llm-caller-public-contract.md：「呼叫端只負責建立 attempt_fn
與 fallback callback；不得各自手寫 429/529 分類器、退避秒數計算或第二層 retry」。任一呼叫端複製
手寫指數退避公式或自建分類器，本守護即變紅，逼其改走 llm_caller 中介層。
"""

from __future__ import annotations

import re
from pathlib import Path

STUDIO = Path(__file__).resolve().parents[2] / "studio"

# 指數退避公式核心識別：base*(2**attempt) 形式。
_BACKOFF_RE = re.compile(r"2\s*\*\*\s*attempt")
# 退避公式允許出現的檔案：
#  - llm_caller.py：LLM 韌性中介層的唯一真相（公式本體＋docstring 範例）。
#  - publisher.py：git/PR/CI 輪詢的網路重試退避，與 LLM 限流退避正交、刻意不共用核心
#    （見 commit a031bd0「標注 publisher._backoff 屬範圍外、刻意不共用核心」）。
_BACKOFF_ALLOW = {"llm_caller.py", "publisher.py"}

# 429／529 分類器的唯一標記：529 過載（overloaded_error）的判別知識只屬 llm_caller；
# 呼叫端若自建分類器勢必再次寫出此 token。它在全樹現況僅出現於 llm_caller.py。
_CLASSIFIER_MARKER = "overloaded_error"
_CLASSIFIER_ALLOW = {"llm_caller.py"}


def _studio_py() -> list[Path]:
    return sorted(STUDIO.glob("*.py"))


def _offenders(pattern_in_text, allow: set[str]) -> set[str]:
    return {
        p.name
        for p in _studio_py()
        if p.name not in allow and pattern_in_text(p.read_text(encoding="utf-8"))
    }


def test_backoff_formula_single_home():
    offenders = _offenders(lambda t: bool(_BACKOFF_RE.search(t)), _BACKOFF_ALLOW)
    assert not offenders, (
        f"指數退避公式 2**attempt 只應在 {sorted(_BACKOFF_ALLOW)}；發現呼叫端複製手寫："
        f"{sorted(offenders)}。請改走 llm_caller.backoff_delay／run_with_retries（見公開契約）。"
    )


def test_classifier_marker_single_home():
    offenders = _offenders(lambda t: _CLASSIFIER_MARKER in t, _CLASSIFIER_ALLOW)
    assert not offenders, (
        f"429／529 分類標記 {_CLASSIFIER_MARKER!r} 只應在 {sorted(_CLASSIFIER_ALLOW)}；"
        f"發現他處複製分類器：{sorted(offenders)}。請改走 llm_caller.classify_* 入口。"
    )


# --- 自證對應 + 排除假綠（CLAUDE.md 鐵則）：偵測器要真能抓到手寫樣本，也要不誤殺正規用法 ---


def test_guard_has_teeth_backoff():
    assert _BACKOFF_RE.search("delay = base * (2 ** attempt)")  # 手寫樣本 → 抓得到
    assert not _BACKOFF_RE.search("delay = backoff_delay(retry_after, n)")  # 正規調用 → 不誤殺


def test_guard_has_teeth_classifier():
    assert _CLASSIFIER_MARKER in 'if "overloaded_error" in body:'  # 手寫分類 → 抓得到
    assert _CLASSIFIER_MARKER not in "sig = classify_failure(exc)"  # 正規調用 → 不誤殺


def test_single_home_files_actually_contain_impl():
    # 反向確認：allowlist 指向的檔案確實有該實作，避免 allowlist 寫錯卻假綠。
    llm = (STUDIO / "llm_caller.py").read_text(encoding="utf-8")
    assert _BACKOFF_RE.search(llm) and _CLASSIFIER_MARKER in llm
