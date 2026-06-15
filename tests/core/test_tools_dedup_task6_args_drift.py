"""任務 #3：反向黑樣本——args 漂移會穿透去重層（已知限制，非 bug）。

去重 key 由 ``dedup_key`` 以 ``sha256(json.dumps(args, sort_keys=True))`` 推導，所以**任何
改變序列化結果的 args 漂移都會讓 key 變化 → 跨 attempt 重放時 cache miss → 副作用重跑**。
這是 ``execute_deduped`` 刻意接受的 at-least-once 設計邊界（寧可多跑一次無害重放，也不
冒「少跑」的靜默資料遺失風險，見 ``DedupCache`` docstring）。

本檔用 ``@pytest.mark.parametrize`` 把三類 LLM 行為漂移釘成明文黑樣本，每個 case 斷言
副作用發生 **2 次**：

  Case1 差一空格   "echo A >> f.txt"          → "echo A  >> f.txt"
  Case2 多一鍵     {"command": ...}           → {"command": ..., "_meta": "v"}
  Case3 value 型別 {"command": ..., "_meta": 1} → {..., "_meta": "1"}（int → str）

【鑑別力契約】若有人「修好」此限制（例如改用語意正規化 key、忽略多餘鍵），這些黑樣本會
從「2 行」變「1 行」而**翻紅**——刻意以此鎖死團隊對現行限制的共識，修掉須同步改測試。

【Case3 為何把型別漂移打在輔助鍵 ``_meta`` 而非 ``command``】``command`` 規格為 string，
傳整數 command 會讓 runner 執行失敗、取不到兩個「成功副作用」。但任務要求覆蓋的是
「value 型別不同」這個 LLM 行為（例如同一參數回 ``1`` vs ``"1"``）——只要把型別漂移放在
**不影響執行的輔助鍵** ``_meta`` 上，``command`` 兩邊保持一致照常 append，``int 1`` 與
``str "1"`` 在 ``json.dumps`` 後序列化必異（``1`` vs ``"1"``）→ digest 變 → 漏命中。
如此忠實覆蓋「value 型別不同」，同時保留 2 次副作用的鑑別力。

注意：本檔僅新增測試，不觸碰任何生產碼。
"""

from __future__ import annotations

import asyncio

import pytest

from studio import config, tools


def _run(coro):
    # task3/task5/task6 各自持有一份 _run（已知技術債，日後一次性遷 conftest，見 PR 說明）。
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _no_sandbox(monkeypatch):
    """關閉 bwrap sandbox，讓 run_bash 在本機直跑、append 真正落地。

    args drift 的斷言是「副作用行數 == 2」，要求 run_bash 真正寫檔；bwrap 缺席環境下
    sandbox 會回一段非 ``_ERROR_PREFIXES`` 開頭的訊息而被誤判成功、append 卻從未發生，
    讓黑樣本失去意義。測試只關心去重對齊，檔案操作仍隔離在 pytest ``tmp_path`` 內。
    """
    monkeypatch.setattr(config, "SANDBOX_ENABLED", False)


# 三類 args 漂移：(case_id, attempt1 的 args, attempt2 重放時漂移後的 args)
# 兩個 attempt 都 append 到 f.txt；漂移使 attempt2 漏命中 → 共 2 行。
_DRIFT_CASES = [
    pytest.param(
        {"command": "echo A >> f.txt"},
        {"command": "echo A  >> f.txt"},  # 命令多一個空格（語意同、字串異）
        id="extra_space",
    ),
    pytest.param(
        {"command": "echo B >> f.txt"},
        {"command": "echo B >> f.txt", "_meta": "v"},  # args 多一個鍵
        id="extra_key",
    ),
    pytest.param(
        {"command": "echo C >> f.txt", "_meta": 1},  # 輔助鍵型別 int
        {"command": "echo C >> f.txt", "_meta": "1"},  # 重放時同鍵變 str（型別漂移）
        id="value_type_drift",
    ),
]


@pytest.mark.parametrize("first_args, drifted_args", _DRIFT_CASES)
def test_BLACK_args_drift_penetrates_dedup_double_side_effect(tmp_path, first_args, drifted_args):
    """已知限制：重放時 args 任一漂移 → 去重 key 變 → 漏命中 → run_bash 副作用跑兩次。

    每個 parametrize case 各取獨立 ``tmp_path``，互不干擾。斷言 f.txt 共 2 行——若此限制
    被「修掉」（漂移仍命中），會只剩 1 行而翻紅，達到黑樣本鑑別力。
    """
    cache = tools.DedupCache()

    cache.new_attempt()  # attempt1：首次執行，副作用落地一次
    _run(tools.execute_deduped("run_bash", first_args, tmp_path, cache))

    cache.new_attempt()  # attempt2：重放，但 LLM 把 args 漂移了
    _run(tools.execute_deduped("run_bash", drifted_args, tmp_path, cache))

    # 限制顯式化：args 漂移使去重漏命中 → append 發生兩次（at-least-once 設計邊界）
    lines = (tmp_path / "f.txt").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2, f"args 漂移未穿透去重（限制疑似被修改），實得 {lines}"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
