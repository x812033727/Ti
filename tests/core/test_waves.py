"""任務依賴解析與波次分層（並行排程的純函式地基）。

不碰 async / cwd / LLM，純驗證 parse_tasks_with_deps 與 build_waves 的邏輯：
- 任務行可帶可選 `#id`、缺則自動編號；依賴行解析成邊；懸空依賴丟棄。
- build_waves 以拓撲分層成波次；同一波內獨立、波次之間循序；循環依賴退回純循序 fallback。
"""

from __future__ import annotations

from studio import config
from studio.orchestrator import StudioSession, build_waves, parse_tasks_with_deps
from studio.roles import BY_KEY


def _ids(waves):
    return [[t["id"] for t in wave] for wave in waves]


# --- parse_tasks_with_deps ---------------------------------------------------


def test_parse_explicit_ids_and_deps():
    text = (
        "任務: #1 建立資料模型\n"
        "任務: #2 實作 API\n"
        "任務: #3 寫前端\n"
        "依賴: #2 -> #1\n"
        "依賴: #3 -> #2\n"
    )
    tasks, edges = parse_tasks_with_deps(text)
    assert [t["id"] for t in tasks] == [1, 2, 3]
    assert tasks[0]["title"] == "建立資料模型"
    assert all(t["status"] == "todo" for t in tasks)
    assert set(edges) == {(2, 1), (3, 2)}


def test_parse_auto_number_when_no_explicit_id():
    text = "任務: 甲\n任務: 乙\n依賴: #2 -> #1\n"
    tasks, edges = parse_tasks_with_deps(text)
    assert [t["id"] for t in tasks] == [1, 2]
    assert [t["title"] for t in tasks] == ["甲", "乙"]
    assert edges == [(2, 1)]


def test_parse_falls_back_to_bullets_without_task_lines():
    text = "計畫：\n- 第一步\n- 第二步\n"
    tasks, edges = parse_tasks_with_deps(text)
    assert [t["title"] for t in tasks] == ["第一步", "第二步"]
    assert [t["id"] for t in tasks] == [1, 2]
    assert edges == []


def test_parse_drops_dangling_and_self_edges():
    text = "任務: #1 A\n任務: #2 B\n依賴: #2 -> #9\n依賴: #1 -> #1\n依賴: #2 -> #1\n"
    _tasks, edges = parse_tasks_with_deps(text)
    # #9 不存在、#1->#1 自環，皆丟棄；只留合法的 #2->#1。
    assert edges == [(2, 1)]


def test_parse_respects_max_tasks_cap(monkeypatch):
    monkeypatch.setattr(config, "MAX_TASKS", 2)
    text = "任務: #1 A\n任務: #2 B\n任務: #3 C\n"
    tasks, _edges = parse_tasks_with_deps(text)
    assert [t["id"] for t in tasks] == [1, 2]


def test_parse_dedupes_conflicting_ids():
    # 兩個都顯式 #1 → 第二個讓位到下一個可用 id，保證唯一。
    text = "任務: #1 A\n任務: #1 B\n"
    tasks, _edges = parse_tasks_with_deps(text)
    assert len({t["id"] for t in tasks}) == 2


# --- build_waves -------------------------------------------------------------


def _tasks(*ids):
    return [{"id": i, "title": f"t{i}", "status": "todo"} for i in ids]


def test_waves_linear_chain_is_fully_sequential():
    tasks = _tasks(1, 2, 3)
    waves = build_waves(tasks, [(2, 1), (3, 2)])
    assert _ids(waves) == [[1], [2], [3]]


def test_waves_all_independent_is_single_wave():
    tasks = _tasks(1, 2, 3)
    waves = build_waves(tasks, [])
    assert _ids(waves) == [[1, 2, 3]]


def test_waves_two_independent_chains_layer_diagonally():
    # 鏈 A: 1 <- 3 ；鏈 B: 2 <- 4。兩鏈獨立 → 每波各取兩鏈同層。
    tasks = _tasks(1, 2, 3, 4)
    waves = build_waves(tasks, [(3, 1), (4, 2)])
    assert _ids(waves) == [[1, 2], [3, 4]]


def test_waves_diamond_dependency():
    # 1 為根，2/3 依賴 1，4 依賴 2 與 3。
    tasks = _tasks(1, 2, 3, 4)
    waves = build_waves(tasks, [(2, 1), (3, 1), (4, 2), (4, 3)])
    assert _ids(waves) == [[1], [2, 3], [4]]


def test_waves_cycle_falls_back_to_per_task_waves():
    tasks = _tasks(1, 2, 3)
    # 1->2->1 形成環；3 獨立。3 先成一波，環內 1、2 退回每任務一波。
    waves = build_waves(tasks, [(1, 2), (2, 1)])
    assert _ids(waves) == [[3], [1], [2]]


def test_waves_ignores_unknown_edge_ids():
    tasks = _tasks(1, 2)
    waves = build_waves(tasks, [(2, 1), (5, 1)])  # (5,1) 指向未知任務 → 忽略
    assert _ids(waves) == [[1], [2]]


def test_waves_empty_tasks():
    assert build_waves([], []) == []


# --- _plan_lanes：支線數自適應（受 PARALLEL_LANES 與 LLM 並發預算雙重約束）-------


class _Stub:
    def __init__(self, role):
        self.role = role

    async def speak(self, prompt, broadcast):  # pragma: no cover - 不會被呼叫
        return "ok"

    async def stop(self):  # pragma: no cover
        pass


def _session(tmp_path, monkeypatch, *, lanes, llm_max):
    """建一個並行啟用、含 3 位 reviewer（qa/senior/security）的 session 供 _plan_lanes 測試。"""
    monkeypatch.setattr(config, "PARALLEL_TASKS_ENABLED", True)
    monkeypatch.setattr(config, "PARALLEL_LANES", lanes)
    monkeypatch.setattr(config, "LLM_MAX_CONCURRENCY", llm_max)
    experts = {k: _Stub(BY_KEY[k]) for k in ("pm", "engineer", "qa", "senior", "security")}

    async def bc(ev):  # pragma: no cover
        pass

    return StudioSession("s", bc, experts=experts, cwd=tmp_path)


def test_plan_lanes_default_budget_matches_parallel_lanes(tmp_path, monkeypatch):
    # 預設：PARALLEL_LANES=3、LLM=9、3 reviewer → 預算=3。5 任務 → 3 條 lane（行為不變）。
    s = _session(tmp_path, monkeypatch, lanes=3, llm_max=9)
    lanes = s._plan_lanes(_tasks(1, 2, 3, 4, 5))
    assert len(lanes) == 3
    assert sorted(t["id"] for ln in lanes for t in ln) == [1, 2, 3, 4, 5]


def test_plan_lanes_llm_budget_caps_high_parallel_lanes(tmp_path, monkeypatch):
    # PARALLEL_LANES 調高到 10 但 LLM=9、3 reviewer → 預算=3 夾住 → 8 任務仍只開 3 條 lane。
    s = _session(tmp_path, monkeypatch, lanes=10, llm_max=9)
    lanes = s._plan_lanes(_tasks(1, 2, 3, 4, 5, 6, 7, 8))
    assert len(lanes) == 3


def test_plan_lanes_scales_up_when_budget_allows(tmp_path, monkeypatch):
    # 同時調高 PARALLEL_LANES=10 與 LLM=30 → 預算=10 → 8 任務自適應開到 8 條 lane。
    s = _session(tmp_path, monkeypatch, lanes=10, llm_max=30)
    lanes = s._plan_lanes(_tasks(1, 2, 3, 4, 5, 6, 7, 8))
    assert len(lanes) == 8


def test_plan_lanes_never_exceeds_wave_size(tmp_path, monkeypatch):
    # 預算充足但任務只有 2 個 → 至多 2 條 lane。
    s = _session(tmp_path, monkeypatch, lanes=10, llm_max=30)
    assert len(s._plan_lanes(_tasks(1, 2))) == 2
