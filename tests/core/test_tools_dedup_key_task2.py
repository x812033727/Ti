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
    """key 形如 ``<tool>:<16 hex>``，前綴工具名便於除錯、後段固定 16 位 16 進位。"""
    key = tools.dedup_key("edit_file", {"path": "a"})
    prefix, _, digest = key.partition(":")
    assert prefix == "edit_file"
    assert len(digest) == 16
    assert all(c in "0123456789abcdef" for c in digest)
