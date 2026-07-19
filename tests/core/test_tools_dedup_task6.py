"""任務 #6（A 段）：重放去重時底層 ``execute`` 僅被呼叫一次（驗收 #1）。

定位：前序 task3/task5 以「真實副作用計數」（append 行數）間接推算去重生效，但無法
證明「底層 ``execute`` 本身沒被多呼叫一次」——若有人改寫 ``execute_deduped`` 讓命中
路徑仍進 ``execute`` 卻在更內層才短路，行數驗收可能仍綠、缺陷漏網。本檔以
``unittest.mock.patch(wraps=...)`` spy 住 ``studio.tools.execute``，**直接斷言
``call_count == 1``**，再疊加「真實副作用行數 == 1」作雙重保護。

【不新增第三方依賴】spy 一律用標準庫 ``unittest.mock.patch``（``wraps`` 保留真實副作用），
不引入 ``pytest-mock``——驗收 #2 硬鎖。

【AsyncMock 自動偵測】``execute`` 是 coroutine function，``patch`` 在 Python ≥3.8 會
自動以 ``AsyncMock`` 替換並 await 被包裹的原函式；本專案 requires-python>=3.11，無需
手動指定 ``new_callable=AsyncMock``。

【#1 命門：兩 attempt 之間必須 ``cache.new_attempt()``】
若省略此呼叫，``_seen[base]`` 不會重置，attempt2 取到的後綴是 ``#1`` 而非 ``#0``，
與 attempt1 的 ``#0`` 不同 key → cache miss → ``execute`` 對同一非冪等呼叫再跑一次
→ ``call_count == 2`` 且副作用行數 == 2，兩個斷言會同時翻紅。此處保留正確的
``new_attempt()`` 呼叫；任何「照抄但漏掉這一行」的實作都會被本測試立刻抓出。
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from studio import config, tools


def _run(coro):
    # 技術債：task3/task5/task6 各自持有一份 _run（與 _no_sandbox），暫不抽進 conftest，
    # 因本 PR 邊界硬鎖「只加測試」，conftest 屬共用基礎設施，留待日後一次性遷移。
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _no_sandbox(monkeypatch):
    """關閉 bwrap sandbox，讓 run_bash 在本機直跑、append 副作用實際落地。

    SANDBOX_ENABLED 預設 True；bwrap 缺席時 run_bash 會回傳「非錯誤前綴」的訊息而被
    誤判成功入快取、且 append 從未發生，使「副作用行數」雙重保護失真。本檔的副作用斷言
    （行數 == 1）要求 run_bash 真正落地，故必須關閉 sandbox；檔案仍隔離在 tmp_path 內。
    """
    monkeypatch.setattr(config, "SANDBOX_ENABLED", False)


def test_replay_run_bash_calls_execute_once(tmp_path):
    """run_bash 跨 attempt 重放：底層 ``execute`` 僅被呼叫 1 次，且 append 僅 1 行。

    spy（``wraps`` 真實 ``execute``）的 context 包住 attempt1 + attempt2 整個序列，
    重放後在 context 外斷言 ``call_count == 1`` AND 檔案行數 == 1（雙重保護）。
    """
    cache = tools.DedupCache()
    cmd = {"command": "echo A >> log.txt"}

    async def one_attempt():
        cache.new_attempt()  # 命門：重置 attempt 內出現序號，重放才能對齊回 #0
        await tools.execute_deduped("run_bash", cmd, tmp_path, cache)

    # wraps=tools.execute 在 patch() 建構時即捕捉原始 reference（此刻尚未替換），
    # 故 mock 命中未快取分支時仍會真正執行副作用。
    with patch("studio.tools.execute", wraps=tools.execute) as mock_execute:
        _run(one_attempt())  # attempt1：未命中 → execute 跑一次、append 一行
        _run(one_attempt())  # attempt2：重放命中快取 → 不再進 execute

    # 雙重保護①：底層 execute 僅被呼叫一次
    assert (
        mock_execute.call_count == 1
    ), f"execute 被呼叫 {mock_execute.call_count} 次，預期 1 次（重放應命中快取不重執行）"
    # 雙重保護②：真實副作用僅發生一次
    assert (tmp_path / "log.txt").read_text().splitlines() == ["A"]


def test_replay_edit_file_calls_execute_once(tmp_path):
    """edit_file 跨 attempt 重放：底層 ``execute`` 僅被呼叫 1 次，檔案僅被改一次。

    edit_file 為非冪等（old 須唯一，重放第二次必失敗）。去重命中後不重進 execute，
    故第二次重放既不報「old 不存在」也不重複替換——以最終內容與 call_count 雙重驗收。
    """
    (tmp_path / "f.txt").write_text("hello world", encoding="utf-8")
    cache = tools.DedupCache()
    args = {"path": "f.txt", "old": "world", "new": "there"}

    async def one_attempt():
        cache.new_attempt()
        return await tools.execute_deduped("edit_file", args, tmp_path, cache)

    with patch("studio.tools.execute", wraps=tools.execute) as mock_execute:
        r1 = _run(one_attempt())  # attempt1：實際替換
        r2 = _run(one_attempt())  # attempt2：重放命中快取 → 回首次成功結果，不重進 execute

    assert (
        mock_execute.call_count == 1
    ), f"execute 被呼叫 {mock_execute.call_count} 次，預期 1 次（重放應命中快取不重執行）"
    # 命中回首次成功結果，而非第二次重跑會得到的「old 不存在」錯誤
    assert r1 == r2
    assert "錯誤" not in r2
    # 真實副作用僅發生一次：替換後內容穩定，未被二次替換或破壞
    assert (tmp_path / "f.txt").read_text() == "hello there"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
