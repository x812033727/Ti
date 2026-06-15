"""QA 獨立驗證（任務 #2）：session 內去重「快取結構 + key 推導」的破壞性測試。

任務 #2 交付物＝`tools.dedup_key`（key 推導）＋分類常數；快取容器（per-session dict）
按架構決策落在 providers（屬 #3/#4 接入）。本檔以 QA 立場驗證「結構與 key 設計本身」
是否站得住，並把已知限制測試化，不讓限制隱形：

驗收對應：
- #4：key 不依賴 tc.id，由「工具名 + 已解析 args」推導，重放仍命中同一 key。
- #5：純記憶體、無外部相依（不 import redis/temporal/db、純函式、可離線、確定性）。
- #2（設計級）：以「key + dict」模擬去重迴圈，證明非冪等工具重放時副作用只跑一次。
- #6：反向黑樣本鎖死已知限制（LLM 重放改 args → 漏命中；key 不含 session → 容器須 per-session）。

刻意與工程師的 test_tools_dedup_key_task2.py 互補，不重疊覆蓋面。
"""

from __future__ import annotations

import json

import pytest

from studio import tools

# ============================================================================
# 驗收 #5：無外部相依 / 純記憶體 / 確定性
# ============================================================================


def test_dedup_key_no_external_dependency_imports():
    """tools 模組不得引入 redis/temporal/外部 DB 客戶端——去重須純記憶體無外部相依。

    黑樣本意義：若有人未來把去重落到 Redis/Temporal（研究員提案、PM 已砍），
    此測試會紅，逼其回到「純記憶體 per-session dict」的最小範圍決策。
    """
    import inspect

    src = inspect.getsource(tools)
    lowered = src.lower()
    for forbidden in ("import redis", "import temporal", "temporalio", "import inngest", "restate"):
        assert forbidden not in lowered, f"去重不得引入外部相依：{forbidden}"


def test_dedup_key_is_pure_and_deterministic():
    """同輸入永遠同輸出（sha256 確定性），無隨機、無時間、無 I/O。

    跑 50 次都相同 → 證明可安全用作 dict key、跨 retry 穩定命中。
    """
    args = {"command": "pytest -q", "n": 3}
    keys = {tools.dedup_key("run_bash", args) for _ in range(50)}
    assert len(keys) == 1


def test_dedup_key_offline_no_network():
    """key 推導不得發網路請求：把 socket.socket 換成地雷，呼叫仍成功。"""
    import socket

    orig = socket.socket

    def _boom(*a, **k):  # 任何嘗試建 socket 都炸
        raise AssertionError("dedup_key 不該碰網路")

    socket.socket = _boom  # type: ignore[assignment]
    try:
        k = tools.dedup_key("edit_file", {"path": "a.py", "old": "x", "new": "y"})
    finally:
        socket.socket = orig  # type: ignore[assignment]
    assert k.startswith("edit_file:")


# ============================================================================
# 驗收 #4：key 推導邊界（不依賴 tc.id，name+args 推導）
# ============================================================================


def test_empty_args_stable_key():
    """空 args（{}）也要產生穩定 key——無參工具重放同樣需命中。"""
    assert tools.dedup_key("run_bash", {}) == tools.dedup_key("run_bash", {})


def test_nested_args_order_independence_recurses():
    """巢狀 dict 的鍵序也要無關（sort_keys 遞迴生效）。

    若只對頂層排序、巢狀層保留輸入序，重放時巢狀順序若變動會假 miss。
    """
    a = tools.dedup_key("run_bash", {"env": {"A": "1", "B": "2"}, "cmd": "x"})
    b = tools.dedup_key("run_bash", {"cmd": "x", "env": {"B": "2", "A": "1"}})
    assert a == b


def test_type_sensitivity_int_vs_str():
    """值的型別不同須產生不同 key（1 vs "1"）——避免語意不同卻誤命中。"""
    assert tools.dedup_key("run_bash", {"x": 1}) != tools.dedup_key("run_bash", {"x": "1"})


def test_unicode_args_keyable():
    """非 ASCII 參數（中文路徑/內容）也要能穩定推導且重放命中。"""
    args = {"path": "報告/結論.md", "content": "你好"}
    assert tools.dedup_key("write_file", args) == tools.dedup_key(
        "write_file", dict(reversed(list(args.items())))
    )


def test_key_via_parse_args_roundtrip_matches():
    """經 parse_args（JSON 字串→dict）後推導，與直接傳 dict 結果一致。

    釘死「key 用已解析 dict、不碰原始 JSON 字串」的契約（架構決策）。
    """
    raw = json.dumps({"command": "ls", "flag": True})
    from_str = tools.dedup_key("run_bash", tools.parse_args(raw))
    from_dict = tools.dedup_key("run_bash", {"flag": True, "command": "ls"})
    assert from_str == from_dict


# ============================================================================
# 驗收 #2（設計級）：以「key + dict」模擬去重迴圈，證明副作用只跑一次
# ============================================================================
#
# 任務 #2 只交付 key + 快取結構；以下用工程師將在 #3/#4 接入的相同邏輯
# （is_idempotent 判分流 + dedup_key 當 dict key + 副作用成功後才寫快取）
# 模擬一輪 speak 內的「首次執行 + retry 重放」，證明結構設計確實達成 exactly-once。


async def _deduped_execute(cache: dict, name: str, args: dict, cwd) -> str:
    """模擬 #3 去重接入：非冪等工具走快取、命中回首次結果、成功後才寫入。"""
    key = None
    if not tools.is_idempotent(name):
        key = tools.dedup_key(name, args)
        if key in cache:
            return cache[key]  # 命中 → 不重執行副作用
    result = await tools.execute(name, args, cwd)
    # 副作用成功後才寫快取（錯誤字串不寫，避免假命中）
    if key is not None and not result.startswith(("錯誤", "工具執行錯誤")):
        cache[key] = result
    return result


@pytest.mark.asyncio
async def test_DESIGN_replay_runs_side_effect_once(tmp_path):
    """非冪等 run_bash 的 append 副作用，在『首次 + 重放』下只發生一次。

    這是驗收 #2 的設計級證據：同 session（同一 cache dict）、同 key 的第二次呼叫
    回首次結果、不再 append。以實際檔案行數驗證副作用計數 == 1。
    """
    cache: dict = {}
    args = {"command": "echo line >> log.txt"}

    r1 = await _deduped_execute(cache, "run_bash", args, tmp_path)
    # 重放：retry 把整輪工具迴圈重跑，args 內容不變（tc.id 即使重生也無關）
    r2 = await _deduped_execute(cache, "run_bash", args, tmp_path)

    assert r1 == r2  # 重放回傳首次結果
    log = (tmp_path / "log.txt").read_text()
    assert log.count("line") == 1, f"副作用應只發生一次，實際={log!r}"


@pytest.mark.asyncio
async def test_DESIGN_idempotent_tool_not_deduped(tmp_path):
    """冪等/讀取型工具不走去重路徑：每次都真的執行，快取維持空（驗收 #3）。"""
    (tmp_path / "a.txt").write_text("v1", encoding="utf-8")
    cache: dict = {}

    out1 = await _deduped_execute(cache, "read_file", {"path": "a.txt"}, tmp_path)
    assert out1 == "v1"
    # read_file 是冪等 → 不入快取
    assert cache == {}

    # 中途檔案被改 → 第二次 read 取到新值（證明沒被去重凍結成舊值）
    (tmp_path / "a.txt").write_text("v2", encoding="utf-8")
    out2 = await _deduped_execute(cache, "read_file", {"path": "a.txt"}, tmp_path)
    assert out2 == "v2"
    assert cache == {}


@pytest.mark.asyncio
async def test_DESIGN_failed_call_not_cached_no_false_hit(tmp_path):
    """失敗（錯誤字串）不寫快取：修好底層後重試應真的執行、不回舊錯誤（驗收 #5）。"""
    cache: dict = {}
    # edit_file 目標不存在 → 回「找不到」錯誤，不應入快取
    args = {"path": "f.txt", "old": "a", "new": "b"}
    err = await _deduped_execute(cache, "edit_file", args, tmp_path)
    assert "找不到" in err
    assert cache == {}, "失敗結果不得寫入快取（否則假命中遮蔽問題）"

    # 補上檔案後重試 → 應真的執行成功，而非回上次的錯誤
    (tmp_path / "f.txt").write_text("a", encoding="utf-8")
    ok = await _deduped_execute(cache, "edit_file", args, tmp_path)
    assert "已修改" in ok
    assert (tmp_path / "f.txt").read_text() == "b"


# ============================================================================
# 驗收 #6：已知限制黑樣本（讓限制顯式可見）
# ============================================================================


@pytest.mark.asyncio
async def test_BLACK_llm_changes_args_misses_dedup(tmp_path):
    """已知限制 #1：LLM 重放時若改變 args，key 不同 → 去重漏命中、副作用跑兩次。

    這不是 bug 而是設計邊界（key 必含 args 才能區分不同呼叫）。本黑樣本把限制釘死：
    若未來有人聲稱「已徹底防住重放」，這條紅燈會提醒『改 args 的重放仍會漏』。
    """
    cache: dict = {}
    await _deduped_execute(cache, "run_bash", {"command": "echo x >> log.txt"}, tmp_path)
    # 重放時 LLM 把命令改了一個字（語意相同但字串不同）→ key 變 → miss
    await _deduped_execute(cache, "run_bash", {"command": "echo x  >> log.txt"}, tmp_path)

    log = (tmp_path / "log.txt").read_text()
    assert log.count("x") == 2, "限制顯式化：args 改變導致去重漏命中，副作用跑兩次"


def test_BLACK_key_does_not_embed_session_scope_via_container(tmp_path):
    """已知限制 #2：key 本身不含 session_id → 跨 session 的同 tool+同 args 產生『相同 key』。

    因此 session scope 必須靠『per-session 獨立 cache 容器』達成，而非靠 key 區分。
    黑樣本鎖死此契約：若有人未來改用全域共享 cache，相同 key 會造成跨 session 假命中。
    這裡同時證明：兩個獨立 dict 容器能正確隔離（同 key 不互相污染）。
    """
    args = {"command": "echo hi"}
    key_session_a = tools.dedup_key("run_bash", args)
    key_session_b = tools.dedup_key("run_bash", args)
    # key 相同 → 證明 key 不帶 session 維度
    assert key_session_a == key_session_b

    # 正確隔離靠『不同容器』：session A 命中不影響 session B
    cache_a: dict = {key_session_a: "A 的首次結果"}
    cache_b: dict = {}
    assert key_session_b not in cache_b, "per-session 獨立容器 → 不會跨 session 假命中"
    assert cache_a[key_session_a] == "A 的首次結果"


def test_BLACK_dedup_key_works_for_idempotent_names_too():
    """限制顯式化：dedup_key 對任何工具名都會算出 key（不自帶分類過濾）。

    亦即『是否去重』的把關完全依賴呼叫端先查 is_idempotent；dedup_key 本身
    不保證只對非冪等工具運作。釘死此分工，避免有人誤以為 dedup_key 會自動跳過冪等工具。
    """
    # 即使是 read_file（冪等），dedup_key 仍正常產出 key——分流責任在呼叫端。
    k = tools.dedup_key("read_file", {"path": "a.txt"})
    assert k.startswith("read_file:")
    # 真正的把關：is_idempotent 必須回 True，呼叫端據此跳過去重。
    assert tools.is_idempotent("read_file") is True
