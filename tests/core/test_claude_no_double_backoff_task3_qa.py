"""任務 #3 QA 守護：Claude（ClaudeSDKClient）路徑無雙層退避。

驗收標準（任務 #3）：
- Claude 路徑無雙層退避的判斷有「書面結論」（註解 / docstring），不留口頭已知限制。
- ClaudeSDKClient 本身不做額外退避；重試由 speak() 層的 run_with_retries 統一管控。

本測試在「不安裝 claude-agent-sdk、不連線」前提下，以原始碼層級守住兩件事：
  1. `_build_client` 確有書面結論（點名 ClaudeSDKClient 無額外退避 / 避免雙層疊乘）。
  2. `_build_client` 的 client 建構區段未注入任何 retry/backoff/max_retries 參數
     （反向確認：若未來有人「好心」加上重試旋鈕，本測試會變紅）。

刻意不驗證真實 SDK 重試行為——Claude 重試屬 Ti SDK 側職責，於本層越界。
（架構決策：寫進註解而非新增行為測試；此處僅做回歸守護。）
"""

from __future__ import annotations

import ast
import inspect
import re

from studio import experts


def _build_client_source() -> str:
    return inspect.getsource(experts._build_client)


def test_written_conclusion_present():
    """書面結論存在：點名 ClaudeSDKClient 無額外退避且避免雙層疊乘。"""
    src = _build_client_source()
    assert "ClaudeSDKClient" in src
    # 結論需明確點名「無額外退避 / 不做額外退避」與「雙層疊乘」風險，缺一即視為口頭含糊。
    assert "不做額外退避" in src, "缺少『ClaudeSDKClient 本身不做額外退避』書面結論"
    assert "疊乘" in src, "缺少『避免雙層疊乘』風險說明"
    # 應交叉指向統一入口 run_with_retries，作為單一退避權威。
    assert "run_with_retries" in src


def test_no_retry_kwarg_in_claude_client_construction():
    """反向守護：Claude client 建構區段不得出現任何 retry/backoff 旋鈕。

    解析 _build_client 的 AST，蒐集所有 keyword 參數名，確認沒有
    max_retries / retries / retry / backoff 之類的重試旋鈕被傳入
    ClaudeSDKClient 或 ClaudeAgentOptions。
    """
    src = inspect.getsource(experts._build_client)
    # dedent：inspect.getsource 對模組級函式不含額外縮排，直接 parse。
    tree = ast.parse(src)

    banned = re.compile(r"retry|retries|backoff|max_retries", re.IGNORECASE)
    offending: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.keyword) and node.arg:
            if banned.search(node.arg):
                offending.append(node.arg)

    assert not offending, (
        f"Claude client 建構區段出現疑似重試旋鈕 {offending}；"
        "重試應僅由 speak() 層 run_with_retries 管控，禁止在此疊第二層退避。"
    )


def test_speak_path_uses_single_retry_authority():
    """正向：發言路徑的唯一退避權威是核心 run_with_retries（搭配 make_retry_config）。

    重試骨幹落在 `_speak_with_retries`（speak() 委派至此），而非 _build_client，
    證明 Claude 退避只有 run_with_retries 這一層。
    """
    retry_src = inspect.getsource(experts.Expert._speak_with_retries)
    assert "run_with_retries" in retry_src
    assert "make_retry_config" in retry_src
    # 反向：_build_client 不得自帶 run_with_retries（退避權威不應下沉到 client 建構）。
    assert "run_with_retries" not in re.sub(
        r"#.*", "", _build_client_source()
    ), "退避權威不應出現在 _build_client 的程式碼（註解引用除外）"
