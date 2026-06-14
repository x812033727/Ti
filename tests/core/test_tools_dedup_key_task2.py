"""任務 #2：session 內去重 key 推導（tools.dedup_key）的單元測試。

鎖定驗收 #4 的核心前提——key 由「工具名 + 已解析 args」推導、不依賴 tc.id，且重放
（同輪工具迴圈重跑、tc.id 重生）仍命中同一 key。此檔為 key 函式提供回歸護欄：
- 同 args（含不同鍵序）→ 同 key（重放命中）；
- 不同工具 / 不同 args → 不同 key（不誤命中）；
- 不接觸 tc.id（純由 name+args 推導）。
"""

from __future__ import annotations

import json

from studio import tools


def test_same_args_same_key():
    """同工具同 args 兩次推導出同一 key——這是重放去重命中的根本。"""
    a = tools.dedup_key("run_bash", {"command": "echo hi"})
    b = tools.dedup_key("run_bash", {"command": "echo hi"})
    assert a == b


def test_key_independent_of_dict_order():
    """鍵序不同但內容相同 → 同 key（sort_keys 生效）。

    黑樣本意義：若改用對原始 JSON 字串 hash，序列化順序差異會產生假 miss、去重失效。
    """
    k1 = tools.dedup_key("edit_file", {"path": "a.py", "old": "x", "new": "y"})
    k2 = tools.dedup_key("edit_file", {"new": "y", "path": "a.py", "old": "x"})
    assert k1 == k2


def test_different_tool_different_key():
    same_args = {"path": "a.py"}
    assert tools.dedup_key("edit_file", same_args) != tools.dedup_key("read_file", same_args)


def test_different_args_different_key():
    assert tools.dedup_key("run_bash", {"command": "ls"}) != tools.dedup_key(
        "run_bash", {"command": "rm -rf /"}
    )


def test_key_does_not_depend_on_tc_id():
    """重放模擬：tc.id 改變、但 name+args 不變 → 同 key 仍命中。

    OpenAI retry 會重生成 tool_call id；本測試證明 key 完全不參照 id，只看 name+args，
    因此重放仍命中同一 key（驗收 #4）。
    """
    args = tools.parse_args(json.dumps({"command": "pytest -q"}))
    key_first = tools.dedup_key("run_bash", args)
    # 重放：同一輪工具迴圈被 _attempt 重跑，tc.id 變了但 args 內容一致
    args_replay = tools.parse_args(json.dumps({"command": "pytest -q"}))
    key_replay = tools.dedup_key("run_bash", args_replay)
    assert key_first == key_replay


def test_key_shape_is_name_prefixed_hex():
    """base key 形如 ``<tool>:<16 hex>``，前綴工具名便於除錯、後段固定 16 位 16 進位。"""
    key = tools.dedup_key("edit_file", {"path": "a"})
    prefix, _, digest = key.partition(":")
    assert prefix == "edit_file"
    assert len(digest) == 16
    assert all(c in "0123456789abcdef" for c in digest)


# --- DedupCache：重放 vs 合法重複的區分（critic 退回的正確性修正）-----------


def test_legit_duplicate_in_same_attempt_not_deduped():
    """核心修正＋反向黑樣本：同一 attempt 內 LLM 合法地下兩次相同 args 的非冪等呼叫，
    必須得到**不同** key（都執行），不可被誤去重。

    這正是純 args hash 的盲點：若第二次誤命中，``echo x >> log`` 只 append 一行、靜默
    吞掉使用者本意（副作用「少跑」）。occurrence 序號修掉此回歸。
    """
    cache = tools.DedupCache()
    cache.new_attempt()
    args = {"command": 'echo "x" >> log.txt'}
    k1 = cache.key_for("run_bash", args)
    k2 = cache.key_for("run_bash", args)  # 同 attempt 內第二次合法呼叫
    assert k1 != k2
    # 第一次寫入快取後，第二次仍 miss → 會真正執行（append 第二行）
    cache.put(k1, "exit=0\n")
    assert cache.get(k2) is None


def test_cross_attempt_replay_hits_same_key():
    """跨 attempt 重放：retry 從頭重跑同序列，同位置的同一呼叫須命中首次結果。

    第一個 attempt 執行並 put；第二個 attempt（new_attempt 重置序號後）重放同序列，
    第 0 次呼叫對齊回同一 ``#0`` key → 命中 → 不重執行副作用。
    """
    cache = tools.DedupCache()
    args = {"command": "python increment.py"}

    cache.new_attempt()
    k_first = cache.key_for("run_bash", args)
    assert cache.get(k_first) is None  # 首次 miss → 執行
    cache.put(k_first, "exit=0\ncount=1")

    cache.new_attempt()  # retry：重置 attempt 內序號，保留 results
    k_replay = cache.key_for("run_bash", args)
    assert k_replay == k_first
    assert cache.get(k_replay) == "exit=0\ncount=1"  # 命中 → 不重執行


def test_legit_duplicate_then_replay_each_aligns():
    """混合情境：attempt 內兩次合法重複各自獨立，重放時逐一對齊（#0↔#0、#1↔#1）。"""
    cache = tools.DedupCache()
    args = {"command": 'echo "x" >> log.txt'}

    cache.new_attempt()
    a0 = cache.key_for("run_bash", args)
    a1 = cache.key_for("run_bash", args)
    cache.put(a0, "r0")
    cache.put(a1, "r1")

    cache.new_attempt()
    b0 = cache.key_for("run_bash", args)
    b1 = cache.key_for("run_bash", args)
    assert (b0, b1) == (a0, a1)
    assert cache.get(b0) == "r0"
    assert cache.get(b1) == "r1"


def test_new_attempt_resets_occurrence_not_results():
    """new_attempt 只重置 attempt 內序號計數，不清掉跨 attempt 的結果快取。"""
    cache = tools.DedupCache()
    cache.new_attempt()
    k = cache.key_for("edit_file", {"path": "a.py", "old": "x", "new": "y"})
    cache.put(k, "已修改 a.py")
    cache.new_attempt()
    assert cache.get(k) == "已修改 a.py"  # results 仍在


def test_failed_call_not_cached_no_false_hit():
    """put 只在副作用成功後呼叫——未 put 的 key 重放時仍 miss（防假命中，呼應 #3）。"""
    cache = tools.DedupCache()
    cache.new_attempt()
    k = cache.key_for("run_bash", {"command": "false"})
    # 模擬失敗：呼叫端不 put
    cache.new_attempt()
    k2 = cache.key_for("run_bash", {"command": "false"})
    assert k2 == k
    assert cache.get(k2) is None  # 未快取 → 重放會重執行
