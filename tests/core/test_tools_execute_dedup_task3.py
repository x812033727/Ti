"""任務 #3：tools.execute_deduped 去重層接入的端到端測試。

鎖定驗收：
- #2 同一非冪等工具同 key 第二次呼叫**不重執行副作用**，回首次結果（以實際檔案 append /
  bash 計數證明只跑一次）；
- #3 冪等/讀取型工具（read_file/write_file/web_fetch）不經去重、行為不變；
- #5 快取寫入點在副作用成功之後——失敗不留假命中（重放仍重試）；
- #6 反向黑樣本：同一 attempt 內 LLM 合法重複的非冪等呼叫不被誤去重（副作用照跑）。
"""

from __future__ import annotations

import asyncio

from studio import tools


def _run(coro):
    return asyncio.run(coro)


def test_replay_runbash_side_effect_runs_once(tmp_path):
    """跨 attempt 重放：同一 run_bash 重放命中快取 → append 只發生一次。"""
    cache = tools.DedupCache()
    cmd = {"command": 'echo line >> log.txt'}

    cache.new_attempt()
    r1 = _run(tools.execute_deduped("run_bash", cmd, tmp_path, cache))

    cache.new_attempt()  # retry：重放整輪迴圈
    r2 = _run(tools.execute_deduped("run_bash", cmd, tmp_path, cache))

    assert r1 == r2  # 回首次結果
    # 副作用只發生一次：log.txt 只有一行
    assert (tmp_path / "log.txt").read_text().splitlines() == ["line"]


def test_replay_edit_file_hits_cache_not_refail(tmp_path):
    """edit_file 重放：第二次本會因 old 已被替換而失敗，去重命中回首次成功結果。"""
    f = tmp_path / "a.txt"
    f.write_text("hello world")
    cache = tools.DedupCache()
    args = {"path": "a.txt", "old": "world", "new": "there"}

    cache.new_attempt()
    r1 = _run(tools.execute_deduped("edit_file", args, tmp_path, cache))
    assert r1 == "已修改 a.txt"
    assert f.read_text() == "hello there"

    cache.new_attempt()
    r2 = _run(tools.execute_deduped("edit_file", args, tmp_path, cache))
    assert r2 == "已修改 a.txt"  # 命中快取，未重執行（否則會回「old 出現 0 次」錯誤）
    assert f.read_text() == "hello there"  # 內容未被二次破壞


def test_idempotent_write_file_not_deduped(tmp_path):
    """write_file 冪等、不納管：每次都實際執行（覆寫），不經快取路徑。"""
    cache = tools.DedupCache()
    cache.new_attempt()
    args = {"path": "out.txt", "content": "v2"}
    # 先放一個會誤命中的「毒」結果：若 write_file 真走快取就會回它（並跳過寫檔）
    base = tools.dedup_key("write_file", args)
    cache.put(f"{base}#0", "假命中：不該被回傳")

    r = _run(tools.execute_deduped("write_file", args, tmp_path, cache))
    assert r == "已寫入 out.txt"  # 沒走快取
    assert (tmp_path / "out.txt").read_text() == "v2"  # 真的寫了


def test_read_file_not_deduped(tmp_path):
    """read_file 唯讀、不納管：直通 execute。"""
    (tmp_path / "r.txt").write_text("content")
    cache = tools.DedupCache()
    cache.new_attempt()
    r = _run(tools.execute_deduped("read_file", {"path": "r.txt"}, tmp_path, cache))
    assert r == "content"


def test_failed_edit_not_cached_allows_retry(tmp_path):
    """失敗不快取（驗收 #5）：edit old 不存在 → 回錯誤、不寫快取；修好檔案後重放能成功。"""
    f = tmp_path / "b.txt"
    f.write_text("no match here")
    cache = tools.DedupCache()
    args = {"path": "b.txt", "old": "MISSING", "new": "x"}

    cache.new_attempt()
    r1 = _run(tools.execute_deduped("edit_file", args, tmp_path, cache))
    assert r1.startswith("錯誤：")  # 失敗
    key = tools.dedup_key("edit_file", args) + "#0"
    assert cache.get(key) is None  # 未留假命中

    # 把 old 補進檔案，重放應真正執行（證明上次失敗沒被快取吞掉）
    f.write_text("MISSING here")
    cache.new_attempt()
    r2 = _run(tools.execute_deduped("edit_file", args, tmp_path, cache))
    assert r2 == "已修改 b.txt"
    assert f.read_text() == "x here"


def test_runbash_nonzero_exit_is_cached(tmp_path):
    """run_bash 非零 exit 仍算副作用已發生 → 入快取，重放不重跑（防重複副作用）。"""
    cache = tools.DedupCache()
    # 指令：append 一行後以非零退出
    cmd = {"command": 'echo x >> c.txt; exit 3'}

    cache.new_attempt()
    r1 = _run(tools.execute_deduped("run_bash", cmd, tmp_path, cache))
    assert "exit=3" in r1

    cache.new_attempt()
    r2 = _run(tools.execute_deduped("run_bash", cmd, tmp_path, cache))
    assert r2 == r1  # 命中快取
    # 副作用只發生一次
    assert (tmp_path / "c.txt").read_text().splitlines() == ["x"]


def test_legit_duplicate_in_attempt_both_run(tmp_path):
    """反向黑樣本（驗收 #6）：同一 attempt 內合法重複的 run_bash append，兩次都要執行。"""
    cache = tools.DedupCache()
    cmd = {"command": 'echo dup >> d.txt'}

    cache.new_attempt()
    _run(tools.execute_deduped("run_bash", cmd, tmp_path, cache))
    _run(tools.execute_deduped("run_bash", cmd, tmp_path, cache))  # 第二次合法重複

    # 兩次都執行 → 兩行
    assert (tmp_path / "d.txt").read_text().splitlines() == ["dup", "dup"]


def test_cache_none_passthrough(tmp_path):
    """cache=None → 完全直通 execute，行為與未接入前一致。"""
    (tmp_path / "p.txt").write_text("z")
    r = _run(tools.execute_deduped("read_file", {"path": "p.txt"}, tmp_path, None))
    assert r == "z"
