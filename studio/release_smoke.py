"""Release smoke 檢查：驗證 release body 含非空頂層 Breaking Changes 區塊。

post-publish smoke（非 pre-tag validator）：只判定「實際 release body 是否含
非空的頂層 Breaking Changes 區塊（heading 即 ``BREAKING_HEADING``）」。判定邏輯**一律重用** SSOT
``studio.release_note.extract_breaking_block``（其內部引用 ``BREAKING_HEADING``），
本模組**不**硬寫任何 heading 字面值，避免 emoji／措辭漂移造成 smoke 與 pre-tag 靜默分歧。

副作用邊界（依架構決策）：
  - ``check_body`` 為純判定函式，失敗只 ``raise ValueError(reason)``，不碰 sys.exit／
    不印任何東西——可在測試中直接 ``pytest.raises(ValueError)``，亦可被未來 programmatic
    呼叫者安全重用。
  - ``main`` / ``__main__`` 才是 CLI 邊界：catch ValueError → 印 body 前 500 字至 stderr →
    sys.exit(1)。
"""

from __future__ import annotations

import os
import sys

from studio.release_note import extract_breaking_block

#: 失敗診斷時印出的 body 前綴長度（依驗收標準 #5）。
_BODY_PREVIEW_LEN = 500


def check_body(body: str) -> None:
    """判定 release body 是否含非空頂層 Breaking Changes 區塊。

    通過則正常返回（None）；缺區塊或內容為空則 ``raise ValueError``。

    判定重用 ``extract_breaking_block``：依 release_note L94–95 ``return body or None``
    的合約，空區塊會被轉成 None（不可能回傳空字串），故 ``not result`` 與 ``is None``
    等價；此處用 ``not result`` 作防禦性寫法，即使未來合約鬆動仍能正確翻紅。
    """
    block = extract_breaking_block(body)
    if not block:
        raise ValueError(
            "release body 缺非空頂層 Breaking Changes 區塊"
            "（heading 須逐字為 release_note.BREAKING_HEADING）"
        )


def main() -> int:
    """CLI 入口：body 優先讀環境變數 ``BODY``，fallback stdin。

    無命令列參數：body 只經 env／stdin 進入（依架構決策刻意不接 argv，避免
    shell 字串解析），故不需 argparse。

    依架構決策，env 傳遞不經 shell 字串解析，避免多行／反斜線／-e 開頭被吞字；
    fallback stdin 保留本地管線測試與手動驗收能力。
    """
    body = os.environ.get("BODY")
    if body is None:
        body = sys.stdin.read()

    try:
        check_body(body)
    except ValueError as exc:
        print(f"release smoke 失敗：{exc}", file=sys.stderr)
        print("--- release body（前 500 字）---", file=sys.stderr)
        print(body[:_BODY_PREVIEW_LEN] or "<empty>", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
