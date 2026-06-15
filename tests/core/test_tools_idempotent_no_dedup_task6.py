"""任務 #6（B/task2）：冪等工具不進去重路徑（綁定驗收 #4）。

驗證 ``write_file`` / ``read_file`` / ``web_fetch`` 走 ``execute_deduped`` 時：
``is_idempotent(name) is True`` → 在 ``key_for`` 之前就直通 ``execute``，**完全不碰快取**。

每條測試的雙重斷言（缺一不可）：
  1. **注毒快取後仍回真實結果**：在「若真的進去重路徑會用到的」base key（``dedup_key + '#0'``）
     上放一個假命中值，斷言回傳是「真實執行結果」而非毒值——證明確實沒查快取。
  2. **不寫入快取**：用 ``unittest.mock.patch.object(cache, 'put', wraps=cache.put)`` 監看
     ``put``，斷言 ``call_count == 0``——證明冪等工具的執行結果不會被寫進去重快取。

不引入任何第三方依賴（不裝 pytest-mock）：spy 一律用內建 ``unittest.mock`` / ``monkeypatch``。
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

from studio import tools

POISON = "假命中：不該被回傳"


def _run(coro):
    return asyncio.run(coro)


def _poison_base_key(cache, name, args):
    """在「若進去重路徑、attempt 內第 0 次出現」會落到的 key 上注毒。

    注毒走公開 ``put``（在安裝 spy 之前呼叫），故不污染後續的 ``put`` call_count；
    若 ``execute_deduped`` 對冪等工具仍誤查快取，這個毒值就會被回傳，斷言 #1 立即翻紅。
    """
    cache.put(tools.dedup_key(name, args) + "#0", POISON)


def test_write_file_not_deduped(tmp_path):
    """write_file（覆寫語意、天然冪等）：注毒後仍真寫真回，put 不被呼叫。"""
    cache = tools.DedupCache()
    args = {"path": "out.txt", "content": "real-content"}
    _poison_base_key(cache, "write_file", args)

    with patch.object(cache, "put", wraps=cache.put) as mock_put:
        cache.new_attempt()
        result = _run(tools.execute_deduped("write_file", args, tmp_path, cache))

    assert result != POISON, "冪等工具竟回傳了快取毒值 → 誤走去重路徑"
    assert result == "已寫入 out.txt"
    # 真實副作用確實發生（檔案被寫入真實內容，而非被快取短路跳過）
    assert (tmp_path / "out.txt").read_text(encoding="utf-8") == "real-content"
    assert mock_put.call_count == 0, "冪等工具不該寫快取"


def test_read_file_not_deduped(tmp_path):
    """read_file（唯讀、無副作用）：注毒後仍回真實檔案內容，put 不被呼叫。"""
    (tmp_path / "in.txt").write_text("real-file-body", encoding="utf-8")
    cache = tools.DedupCache()
    args = {"path": "in.txt"}
    _poison_base_key(cache, "read_file", args)

    with patch.object(cache, "put", wraps=cache.put) as mock_put:
        cache.new_attempt()
        result = _run(tools.execute_deduped("read_file", args, tmp_path, cache))

    assert result != POISON, "冪等工具竟回傳了快取毒值 → 誤走去重路徑"
    assert result == "real-file-body"
    assert mock_put.call_count == 0, "冪等工具不該寫快取"


def test_web_fetch_not_deduped(tmp_path, monkeypatch):
    """web_fetch（唯讀）：注毒後仍回真實抓取結果，put 不被呼叫。

    以 ``monkeypatch.setattr(tools, '_research_fetch', ...)`` 餵假回應免真連線（同 task5 模式）。
    """

    async def fake_fetch(url):
        return f"[HTTP 200] {url}\nreal-body"

    monkeypatch.setattr(tools, "_research_fetch", fake_fetch)

    cache = tools.DedupCache()
    args = {"url": "https://example.com/x"}
    _poison_base_key(cache, "web_fetch", args)

    with patch.object(cache, "put", wraps=cache.put) as mock_put:
        cache.new_attempt()
        result = _run(tools.execute_deduped("web_fetch", args, tmp_path, cache))

    assert result != POISON, "冪等工具竟回傳了快取毒值 → 誤走去重路徑"
    assert result == "[HTTP 200] https://example.com/x\nreal-body"
    assert mock_put.call_count == 0, "冪等工具不該寫快取"


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
