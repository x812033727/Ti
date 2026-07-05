"""任務 #5 驗收：_derive_latency 四桶聚合＋事件 payload 形狀＋finish_session 整合。

對應驗收標準：
  - 標準 #1（事件形狀）：events.token_usage(...) 不傳 duration_ms 時 payload 無此鍵、
    傳入時等於給定值。
  - 標準 #2（聚合）：_derive_latency 對含 duration_ms 的事件以 provider/model/speaker
    三維聚合，count/sum_ms/max_ms 正確、avg_ms = sum_ms // count（整數除法）。
  - 標準 #3（回溯相容）：舊事件（payload 無 duration_ms）不計入 latency 四桶，
    _derive_token_usage 對同批事件的 calls/tokens 聚合不受影響。
  - 標準 #4（finish_session 整合）：收尾後 meta 頂層同時有 token_usage 與 latency。

與 tests/core/test_history.py 同樣走 tmp_path + monkeypatch.setattr(config, "HISTORY_ROOT", ...)，
不開 server、不打真 LLM。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from studio import config, events, history

# --- fixtures / helpers ---------------------------------------------------


@pytest.fixture(autouse=True)
def _tmp_history(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HISTORY_ROOT", tmp_path / "history")


def _token_usage_event(
    *,
    sid: str = "s",
    provider: str,
    model: str,
    speaker: str,
    duration_ms: int | None,
    prompt_tokens: int = 100,
    completion_tokens: int = 20,
    total_tokens: int = 120,
    cost_usd: float | None = 0.0,
    task_id: int | None = None,
) -> dict:
    """組一個 history jsonl 用的事件 dict。duration_ms=None 表示「舊事件」不帶此鍵。"""
    payload: dict = {
        "speaker": speaker,
        "provider": provider,
        "model": model,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "cost_usd": cost_usd,
        "cache_read": 0,
        "cache_write": 0,
    }
    if duration_ms is not None:
        payload["duration_ms"] = duration_ms
    if task_id is not None:
        payload["task_id"] = task_id
    return {"type": "token_usage", "session_id": sid, "ts": 0, "payload": payload}


# 預期四桶欄位集合（_derive_latency 回傳的固定鍵組）
_LATENCY_FIELDS = ("count", "sum_ms", "max_ms", "avg_ms")


def _assert_latency_bucket(actual: dict, count: int, sum_ms: int, max_ms: int) -> None:
    """驗證單一桶的 count/sum_ms/max_ms，並自動驗 avg_ms == sum_ms // count（count>0 時）。"""
    assert set(actual) == set(_LATENCY_FIELDS), (
        f"latency bucket 欄位不對：actual={set(actual)}, expected={set(_LATENCY_FIELDS)}"
    )
    assert actual["count"] == count, f"count={actual['count']} expected={count}"
    assert actual["sum_ms"] == sum_ms, f"sum_ms={actual['sum_ms']} expected={sum_ms}"
    assert actual["max_ms"] == max_ms, f"max_ms={actual['max_ms']} expected={max_ms}"
    if count > 0:
        assert actual["avg_ms"] == sum_ms // count, (
            f"avg_ms={actual['avg_ms']} expected={sum_ms // count} "
            f"(sum_ms={sum_ms}, count={count}, 整數除法)"
        )
    else:
        assert actual["avg_ms"] == 0


# --- 標準 #2：正樣本——四桶聚合正確 --------------------------------------


def test_derive_latency_aggregates_four_buckets_across_providers_models_speakers():
    """四桶（total / by_provider / by_model / by_role）聚合 count/sum_ms/max_ms/avg_ms 全對。

    設計意圖：跨 2 個 provider × 2 個 model × 2 個 speaker，故意挑選會產生
    「整除後餘數」（engineer 1300/3 = 433 餘 1）以守住 avg_ms 是「整數除法、
    非四捨五入」的隱性合約。
    """
    # 取整除：total 1600/4=400 整除；engineer 1300/3=433（不整除）刻意驗 floor 行為。
    evs = [
        _token_usage_event(provider="openai", model="gpt-5", speaker="engineer", duration_ms=500),
        _token_usage_event(provider="openai", model="gpt-5", speaker="pm", duration_ms=300),
        _token_usage_event(
            provider="claude", model="claude-opus", speaker="engineer", duration_ms=700
        ),
        _token_usage_event(
            provider="claude", model="claude-opus", speaker="engineer", duration_ms=100
        ),
    ]

    lat = history._derive_latency(evs)

    # 形狀：四桶齊全 + 子桶齊全
    assert set(lat) == {"total", "by_provider", "by_model", "by_role"}
    assert set(lat["by_provider"]) == {"openai", "claude"}
    assert set(lat["by_model"]) == {"gpt-5", "claude-opus"}
    assert set(lat["by_role"]) == {"engineer", "pm"}

    # total: 4 筆、1600ms、最大 700、avg 400
    _assert_latency_bucket(lat["total"], count=4, sum_ms=1600, max_ms=700)
    # by_provider
    _assert_latency_bucket(lat["by_provider"]["openai"], count=2, sum_ms=800, max_ms=500)
    _assert_latency_bucket(lat["by_provider"]["claude"], count=2, sum_ms=800, max_ms=700)
    # by_model
    _assert_latency_bucket(lat["by_model"]["gpt-5"], count=2, sum_ms=800, max_ms=500)
    _assert_latency_bucket(lat["by_model"]["claude-opus"], count=2, sum_ms=800, max_ms=700)
    # by_role（engineer 故意不整除，驗 avg_ms = 1300 // 3 = 433，非四捨五入 433.33）
    _assert_latency_bucket(lat["by_role"]["engineer"], count=3, sum_ms=1300, max_ms=700)
    _assert_latency_bucket(lat["by_role"]["pm"], count=1, sum_ms=300, max_ms=300)


def test_derive_latency_empty_input_returns_zero_buckets():
    """空事件串流（無任何 token_usage 事件）→ 四桶皆 count=0、sum/max/avg 全 0。"""
    evs = [
        {"type": "phase_change", "session_id": "s", "ts": 0, "payload": {"phase": "拆解"}},
        {"type": "expert_message", "session_id": "s", "ts": 0, "payload": {"text": "hi"}},
    ]
    lat = history._derive_latency(evs)
    _assert_latency_bucket(lat["total"], count=0, sum_ms=0, max_ms=0)
    assert lat["by_provider"] == {}
    assert lat["by_model"] == {}
    assert lat["by_role"] == {}


def test_derive_latency_skips_zero_negative_and_invalid_duration_with_value_fallback():
    """邊界：duration_ms=0 合法計入；負數/字串等無效值經 _int_ms 歸 0 仍計入。

    目的：守住 _int_ms 的容錯行為——回溯舊檔案若有人塞了 -1 或字串，行為可預期
    （count 仍 +1，但 sum/max 不變），不丟例外、不靜默丟事件。
    """
    evs = [
        _token_usage_event(provider="openai", model="gpt-5", speaker="engineer", duration_ms=500),
        _token_usage_event(provider="openai", model="gpt-5", speaker="engineer", duration_ms=0),
        _token_usage_event(provider="openai", model="gpt-5", speaker="engineer", duration_ms=-1),
        # 故意塞字串；_int_ms 應解析失敗 → 0
        {
            "type": "token_usage",
            "session_id": "s",
            "ts": 0,
            "payload": {
                "speaker": "engineer",
                "provider": "openai",
                "model": "gpt-5",
                "duration_ms": "garbage",
            },
        },
    ]
    lat = history._derive_latency(evs)
    # 4 筆皆計入 count；sum=500（後三筆加 0）；max=500；avg=125（500//4）
    _assert_latency_bucket(lat["total"], count=4, sum_ms=500, max_ms=500)
    _assert_latency_bucket(lat["by_role"]["engineer"], count=4, sum_ms=500, max_ms=500)


# --- 標準 #3：回溯相容——舊事件不計入 latency，但不影響 token_usage 聚合 ---------


def test_legacy_events_without_duration_ms_excluded_from_latency_but_kept_in_token_usage():
    """混入無 duration_ms 的舊事件：latency 四桶 count 不計入；token_usage 的 calls/tokens 照算。

    行為對照：
      - latency：只數含 duration_ms 的事件 → total count=2（只看 openai×1 + claude×1）
      - token_usage：四筆全部計入 calls → total calls=4
    """
    evs = [
        # 含 duration_ms 的新事件（2 筆）
        _token_usage_event(
            provider="openai",
            model="gpt-5",
            speaker="engineer",
            duration_ms=400,
            prompt_tokens=100,
            completion_tokens=20,
            total_tokens=120,
        ),
        _token_usage_event(
            provider="claude",
            model="claude-opus",
            speaker="pm",
            duration_ms=200,
            prompt_tokens=50,
            completion_tokens=10,
            total_tokens=60,
        ),
        # 不含 duration_ms 的舊事件（2 筆）：應完全不計入 latency
        _token_usage_event(
            provider="openai",
            model="gpt-5",
            speaker="engineer",
            duration_ms=None,
            prompt_tokens=70,
            completion_tokens=30,
            total_tokens=100,
        ),
        _token_usage_event(
            provider="claude",
            model="claude-opus",
            speaker="engineer",
            duration_ms=None,
            prompt_tokens=30,
            completion_tokens=5,
            total_tokens=35,
        ),
    ]

    lat = history._derive_latency(evs)
    tu = history._derive_token_usage(evs)

    # latency：只看到 2 筆有 duration_ms 的事件
    _assert_latency_bucket(lat["total"], count=2, sum_ms=600, max_ms=400)
    _assert_latency_bucket(lat["by_provider"]["openai"], count=1, sum_ms=400, max_ms=400)
    _assert_latency_bucket(lat["by_provider"]["claude"], count=1, sum_ms=200, max_ms=200)
    _assert_latency_bucket(lat["by_role"]["engineer"], count=1, sum_ms=400, max_ms=400)
    _assert_latency_bucket(lat["by_role"]["pm"], count=1, sum_ms=200, max_ms=200)
    # 舊事件既不給 openai×2 也不給 claude×2，count 必須仍是 1
    assert lat["by_provider"]["openai"]["count"] == 1
    assert lat["by_provider"]["claude"]["count"] == 1

    # token_usage：四筆全計入（與改動前語義一致）
    assert tu["total"]["calls"] == 4
    assert tu["total"]["prompt"] == 100 + 50 + 70 + 30  # 250
    assert tu["total"]["completion"] == 20 + 10 + 30 + 5  # 65
    assert tu["total"]["total"] == 120 + 60 + 100 + 35  # 315
    assert tu["by_provider"]["openai"]["calls"] == 2
    assert tu["by_provider"]["openai"]["total"] == 120 + 100  # 220
    assert tu["by_provider"]["claude"]["calls"] == 2
    assert tu["by_provider"]["claude"]["total"] == 60 + 35  # 95
    assert tu["by_role"]["engineer"]["calls"] == 3
    assert tu["by_role"]["pm"]["calls"] == 1


# --- 標準 #1：events.token_usage(...) 的 duration_ms payload 形狀 --------------


def test_events_token_usage_omits_duration_ms_when_not_provided():
    """events.token_usage(...) 不傳 duration_ms 時，payload 內不可有此鍵。

    這是回溯相容的寫入端對應：舊讀者只認得「有此鍵才視為有 latency」，不要讓 None 漏成
    payload["duration_ms"] = None 混淆下游。
    """
    ev = events.token_usage(
        "s",
        "engineer",
        "openai",
        "gpt-5",
        10,
        5,
        15,
        cost_usd=0.01,
    )
    assert ev.type == events.EventType.TOKEN_USAGE
    payload = ev.to_dict()["payload"]
    assert "duration_ms" not in payload, f"未傳 duration_ms 時 payload 不應含此鍵，got: {payload!r}"


def test_events_token_usage_keeps_explicit_duration_ms_value():
    """events.token_usage(..., duration_ms=1234) → payload["duration_ms"] == 1234（值精確）。"""
    ev = events.token_usage(
        "s",
        "engineer",
        "openai",
        "gpt-5",
        10,
        5,
        15,
        cost_usd=0.01,
        duration_ms=1234,
    )
    assert ev.to_dict()["payload"]["duration_ms"] == 1234

    # 額外：傳 None 視同不傳（與上一測試同語義）
    ev_none = events.token_usage(
        "s",
        "engineer",
        "openai",
        "gpt-5",
        10,
        5,
        15,
        duration_ms=None,
    )
    assert "duration_ms" not in ev_none.to_dict()["payload"]


# --- 標準 #4：finish_session 整合——meta 同時有 token_usage 與 latency --------


def test_finish_session_meta_has_top_level_token_usage_and_latency_keys():
    """真實跑 start_session → record_event → finish_session，meta 頂層同時有兩鍵。

    順手驗證 _derive_token_usage 與 _derive_latency 在 finish_session 內已被串接，
    且舊事件（無 duration_ms）依然能讓 latency 鍵存在但 count=0（不丟鍵、不報錯）。
    """
    sid = "lat-finish"
    history.start_session(sid, "需求：寫一個 CLI")

    # 一筆含 duration_ms 的新事件
    history.record_event(
        sid,
        _token_usage_event(
            sid=sid,
            provider="openai",
            model="gpt-5",
            speaker="engineer",
            duration_ms=250,
            prompt_tokens=10,
            completion_tokens=5,
            total_tokens=15,
        ),
    )
    # 一筆無 duration_ms 的舊事件
    history.record_event(
        sid,
        _token_usage_event(
            sid=sid,
            provider="claude",
            model="claude-opus",
            speaker="pm",
            duration_ms=None,
            prompt_tokens=20,
            completion_tokens=10,
            total_tokens=30,
        ),
    )
    # 收尾事件（finish_session 仰賴 type=done 推 status）
    history.record_event(
        sid, {"type": "done", "session_id": sid, "ts": 0, "payload": {"completed": True}}
    )

    meta = history.finish_session(sid)
    assert meta is not None
    # 頂層兩鍵皆存在
    assert "token_usage" in meta, f"meta 缺 token_usage 鍵：keys={list(meta)}"
    assert "latency" in meta, f"meta 缺 latency 鍵：keys={list(meta)}"

    # token_usage：兩筆都算入
    assert meta["token_usage"]["total"]["calls"] == 2
    assert meta["token_usage"]["total"]["prompt"] == 30  # 10+20
    assert meta["token_usage"]["total"]["total"] == 45  # 15+30

    # latency：僅 1 筆含 duration_ms（250ms），但 latency 鍵仍存在且 by_* 不為空
    assert meta["latency"]["total"]["count"] == 1
    assert meta["latency"]["total"]["sum_ms"] == 250
    assert meta["latency"]["total"]["max_ms"] == 250
    assert meta["latency"]["total"]["avg_ms"] == 250
    # by_role.engineer 該筆；by_role.pm（舊事件）不在 latency 出現
    assert meta["latency"]["by_role"]["engineer"]["count"] == 1
    assert "pm" not in meta["latency"]["by_role"]
    # by_provider.openai 出現；claude 因舊事件沒 latency 故不出現
    assert meta["latency"]["by_provider"]["openai"]["count"] == 1
    assert "claude" not in meta["latency"]["by_provider"]


def test_finish_session_meta_latency_present_even_when_all_events_legacy():
    """全舊事件（皆無 duration_ms）收尾後 meta 仍有 latency 鍵，只是 count=0。"""
    sid = "lat-legacy-only"
    history.start_session(sid, "全舊事件")

    history.record_event(
        sid,
        _token_usage_event(
            sid=sid,
            provider="openai",
            model="gpt-5",
            speaker="engineer",
            duration_ms=None,
            prompt_tokens=5,
            completion_tokens=2,
            total_tokens=7,
        ),
    )
    history.record_event(
        sid, {"type": "done", "session_id": sid, "ts": 0, "payload": {"completed": True}}
    )

    meta = history.finish_session(sid)
    assert meta is not None
    assert "latency" in meta
    assert meta["latency"]["total"]["count"] == 0
    assert meta["latency"]["total"]["sum_ms"] == 0
    assert meta["latency"]["total"]["max_ms"] == 0
    assert meta["latency"]["total"]["avg_ms"] == 0
    # token_usage 照常聚合
    assert meta["token_usage"]["total"]["calls"] == 1
    assert meta["token_usage"]["total"]["total"] == 7


# --- 標準 #5：離線 e2e——finish_session 後 meta 有 latency 頂層鍵 ----------------


@pytest.fixture
def _offline_client(tmp_path, monkeypatch):
    """最小離線 TestClient：沿用 test_offline_e2e.py 的 fixture 模式，
    不打真 LLM（OFFLINE_MODE=True），讓 finish_session 真實跑到。
    HISTORY_ROOT 在 _tmp_history（autouse）設好後由此 fixture 覆寫至同 tmp_path 子目錄，
    兩者皆為 monkeypatch 暫時設定，測試結束自動還原。
    """
    monkeypatch.setattr(config, "ACCESS_PASSWORD", "")
    monkeypatch.setattr(config, "OFFLINE_MODE", True)
    monkeypatch.setattr(config, "PARALLEL_TASKS_ENABLED", False)
    monkeypatch.setattr(config, "OFFLINE_DELAY", 0.0)
    monkeypatch.setattr(config, "DEBATE_ROUNDS", 1)
    monkeypatch.setattr(config, "HUDDLE_ENABLED", False)
    monkeypatch.setattr(config, "REFLEXION_ENABLED", False)
    monkeypatch.setattr(config, "SELF_REFINE_ITERS", 0)
    monkeypatch.setattr(config, "OBJECTIVE_GATE", "0")
    monkeypatch.setattr(config, "WORKSPACE_ROOT", tmp_path / "ws")
    # 覆寫 autouse _tmp_history 設定的路徑：同一 tmp_path 下的 hist 子目錄
    monkeypatch.setattr(config, "HISTORY_ROOT", tmp_path / "hist")
    from studio.server import app

    return TestClient(app, client=("127.0.0.1", 12345))


def test_offline_e2e_meta_has_latency_key(_offline_client):
    """離線端到端：真實 session 收尾後，/api/history/{sid}/events 回傳的 meta
    必須含頂層 latency 鍵，且結構完整（total/by_provider/by_model/by_role，
    各桶有 count/sum_ms/max_ms/avg_ms）。

    fake 專家不帶 duration_ms，故 latency.total.count == 0；但 latency 鍵本身
    必須存在（finish_session 無論有無 duration_ms 事件都寫 meta["latency"]）。
    """
    sid = None
    with _offline_client.websocket_connect("/ws") as ws:
        ws.send_json({"requirement": "做一個加法函式"})
        for _ in range(500):
            ev = ws.receive_json()
            if sid is None:
                sid = ev.get("session_id")
            if ev["type"] in ("done", "error"):
                break

    assert sid is not None, "應取得 session_id"

    resp = _offline_client.get(f"/api/history/{sid}/events")
    assert resp.status_code == 200, f"history 端點失敗：{resp.status_code} {resp.text}"
    data = resp.json()
    meta = data.get("meta", {})

    # 頂層兩鍵必須平行存在（不可 latency 塞進 token_usage 內）
    assert "latency" in meta, f"meta 缺 latency 鍵，實有：{sorted(meta)}"
    assert "token_usage" in meta, f"meta 缺 token_usage 鍵，實有：{sorted(meta)}"

    lat = meta["latency"]
    # 四個子桶齊全
    assert set(lat) == {"total", "by_provider", "by_model", "by_role"}, (
        f"latency 應有 total/by_provider/by_model/by_role，got {sorted(lat)}"
    )
    # total 桶欄位齊全
    for field in _LATENCY_FIELDS:
        assert field in lat["total"], f"latency.total 缺欄位 {field!r}"

    # fake 專家無 duration_ms → count=0，但數值必須合理（非負）
    assert lat["total"]["count"] >= 0
    assert lat["total"]["sum_ms"] >= 0
    assert lat["total"]["max_ms"] >= 0
    assert lat["total"]["avg_ms"] >= 0
    # count=0 時 avg_ms 須為 0（_finalize_latency_bucket 保證）
    if lat["total"]["count"] == 0:
        assert lat["total"]["avg_ms"] == 0, "count=0 時 avg_ms 必須為 0"
