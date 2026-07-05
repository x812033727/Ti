"""任務 #5 QA 補強：non-Claude 路徑、baseline 等價、跨場合併、grep 落地。

與 tests/test_history_latency.py（QA 自驗最初版）互補——後者全在 history/events 端驗證；
本檔專門填補以下盲點：

  A) **providers.py 非 Claude 路徑的 perf_counter 計時**：
     整輪工具迴圈含「多步 _chat + 工具執行」的 wall-clock，duration_ms 必須包住整輪，
     不只最後一次 _chat 的單點時長。Architect 強調「整輪」語意與 Claude 端的
     `duration_api_ms`（單次 API 通訊）刻意不同，須被測試釘住。
  B) **Baseline 等價**：
     「混入無 duration 舊事件時，token_usage 聚合結果與改動前完全一致」最強形式——
     不是各自斷言 calls/total，而是直接組出兩批事件並 diff 整本 dict。
  C) **跨場合併 sum/count 安全性**：
     Architect 列為「本需求最容易踩坑處」。把兩場 session 的 latency.sum_ms 與 count
     相加再重導 avg，必須等於把事件合併成一場後 _derive_latency 的結果，
     才不會在 /api/metrics 跨場聚合時失真。
  D) **acceptance #1 的 grep 落地**：
     「grep `duration_ms` 可在 events.py 命中」這條以檔案內容字串掃描直接鎖死，
     防止有人誤把參數搬到非預期位置（如 payload 內層 dict）。
  E) **離線 e2e 額外正向案例**：
     既有 e2e 因 fake 專家無 duration 故 count==0 過於軟；本檔在 history 端直接驗：
     「只要任何 token_usage 事件帶 duration_ms，meta.latency.total.count >= 1」——
     不開 server、不打 LLM，跑 finish_session 真實序列化路徑。
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from studio import config, events, history, providers
from studio.roles import BY_KEY

# --- 共用 fixtures / helpers ------------------------------------------------


@pytest.fixture(autouse=True)
def _tmp_history(tmp_path, monkeypatch):
    """與 tests/test_history_latency.py 同步：HISTORY_ROOT 指向 tmp，測後自動還原。"""
    monkeypatch.setattr(config, "HISTORY_ROOT", tmp_path / "history")


def _tu_event(
    provider: str,
    model: str,
    speaker: str,
    *,
    duration_ms: int | None,
    prompt_tokens: int = 100,
    completion_tokens: int = 20,
    total_tokens: int = 120,
) -> dict:
    payload: dict = {
        "speaker": speaker,
        "provider": provider,
        "model": model,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "cost_usd": 0.0,
        "cache_read": 0,
        "cache_write": 0,
    }
    if duration_ms is not None:
        payload["duration_ms"] = duration_ms
    return {"type": "token_usage", "session_id": "s", "ts": 0, "payload": payload}


# A) --- non-Claude 路徑的 perf_counter「整輪工具迴圈」語意 ----------------------


def _openai_msg(content=None, tool_calls=None, usage=None):
    """假 OpenAI 回應。usage=None 模擬「provider 沒回用量」→ providers 端 calls 不加、
    整輪結束 usage["calls"]==0 時不發 token_usage 事件。"""
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=tool_calls))],
        usage=usage,
    )


def _openai_usage(p, c, t):
    return SimpleNamespace(prompt_tokens=p, completion_tokens=c, total_tokens=t)


def _openai_tc(id, name, arguments):
    return SimpleNamespace(id=id, function=SimpleNamespace(name=name, arguments=arguments))


class _FakeChatWithSleep:
    """注入的 chat：依序吐回 responses，每次吐之前 sleep 可設秒數以模擬 LLM/工具耗時。

    為什麼需要 sleep：否則 _run_loop 跑太快 wall-clock ≈ 0，無法分辨「整輪包住多步」
    與「只在最後一步計時」的差異。
    """

    def __init__(self, responses_with_sleep_s):
        # responses_with_sleep_s：list[(resp, sleep_s_before_this_resp)]
        self._steps = responses_with_sleep_s
        self.seen = []

    async def __call__(self, messages, tools, model, **_kw):
        self.seen.append({"messages": list(messages), "tools": tools, "model": model})
        resp, sleep_s = self._steps[len(self.seen) - 1]
        if sleep_s > 0:
            await asyncio.sleep(sleep_s)
        return resp


def _collect_bucket():
    bucket = []

    async def broadcast(ev):
        bucket.append(ev)

    return bucket, broadcast


@pytest.mark.asyncio
async def test_non_claude_path_duration_ms_wraps_entire_tool_loop(tmp_path, monkeypatch):
    """核心正樣本：整輪含「step1 sleep(0.10s) + tool 執行 + step2 sleep(0.20s)」，
    結束後 events.token_usage.duration_ms 必須 ≥ 0.30s（≈ 整輪 wall-clock），
    不會被縮成單次 chat 的時長。

    為了不讓 CI 不穩，這裡只斷 ≥0.25（留 50ms 浮動）；亦即守住「至少含最後一步而非單步」的強語意。
    """
    # 假 execute_deduped：簽名必須對齊真實 `async def execute_deduped(name, args, cwd, cache)`
    # （providers.py 工具迴圈以 4 位置參數 await 呼叫），否則測試自己 TypeError 炸掉。
    import studio.tools as tools_module

    async def _fake_execute_deduped(name, args, cwd, cache):
        return f"ok:{name}"

    monkeypatch.setattr(tools_module, "execute_deduped", _fake_execute_deduped)

    # 兩步工具迴圈：①工具呼叫（前置 sleep 0.10s）②最終文字回應（前置 sleep 0.20s）
    chat = _FakeChatWithSleep(
        [
            (
                _openai_msg(
                    tool_calls=[_openai_tc("c1", "write_file", '{"path": "a.py", "content": "x"}')],
                    usage=_openai_usage(100, 0, 100),
                ),
                0.10,
            ),
            (
                _openai_msg(content="完成", usage=_openai_usage(30, 20, 50)),
                0.20,
            ),
        ]
    )

    # 取消硬逾時：以便 fake chat 的 sleep 真的過完那 0.30s
    monkeypatch.setattr(config, "TURN_HARD_TIMEOUT", 0)

    expert = providers.OpenAIExpert(
        BY_KEY["engineer"], "sess", tmp_path, chat=chat, model="m", provider="openai"
    )
    bucket, broadcast = _collect_bucket()

    out = await expert.speak("做 a.py", broadcast)
    assert out == "完成"

    tu_events = [e for e in bucket if e.type == events.EventType.TOKEN_USAGE]
    assert len(tu_events) == 1, f"應該只廣播一次 token_usage，實際 {len(tu_events)}"

    duration_ms = tu_events[0].payload.get("duration_ms")
    assert isinstance(duration_ms, int), (
        f"duration_ms 必須是 int（int() 截斷秒×1000），got {type(duration_ms).__name__}="
        f"{duration_ms!r}"
    )
    # 整輪含 0.10s + 0.20s = 0.30s；int 截斷為 300ms。容忍 ±50ms 浮動守住「整輪」強語意。
    assert 250 <= duration_ms <= 800, (
        f"duration_ms={duration_ms} 不在合理整輪範圍（250~800ms），"
        f"可能是單步計時或 perf_counter 用錯"
    )

    # 帶 outgoing 給 history._derive_latency 看，count=1、sum=duration_ms
    dur = tu_events[0].payload["duration_ms"]
    evs = [
        {
            "type": "token_usage",
            "session_id": "s",
            "ts": 0,
            "payload": {**tu_events[0].payload, "speaker": "engineer"},
        }
    ]
    lat = history._derive_latency(evs)
    assert lat["total"]["count"] == 1
    assert lat["total"]["sum_ms"] == dur
    assert lat["total"]["max_ms"] == dur
    assert lat["total"]["avg_ms"] == dur  # 1 筆 → avg == sum == max


@pytest.mark.asyncio
async def test_non_claude_path_skips_token_usage_event_when_no_usage_reported(
    tmp_path, monkeypatch
):
    """邊界：provider 回應全程沒帶 usage（usage=None）→ usage["calls"]==0 →
    整輪結束不發 token_usage。duration_ms 即便算完也一併丟棄（不污染歷史）。

    第 1 版誤用「chat 拋 RuntimeError」構造此邊界——但 retry 骨幹對非限流的任意例外
    是直接往外拋（不吞、不 fallback），前提不成立；正確的 calls==0 路徑是
    「回應成功但無 usage」（getattr(resp, "usage", None) is None）。
    """
    monkeypatch.setattr(config, "TURN_HARD_TIMEOUT", 0)

    chat = _FakeChatWithSleep([(_openai_msg(content="答", usage=None), 0)])
    expert = providers.OpenAIExpert(BY_KEY["engineer"], "sess", tmp_path, chat=chat, model="m")
    bucket, broadcast = _collect_bucket()

    out = await expert.speak("x", broadcast)
    assert out == "答"
    tu_events = [e for e in bucket if e.type == events.EventType.TOKEN_USAGE]
    assert tu_events == [], (
        f"無 usage 回報時不該發 token_usage（連 duration 也不留）：{tu_events!r}"
    )


# B) --- Baseline 等價 --------------------------------------------------------


def test_token_usage_aggregation_byte_identical_with_or_without_duration_field():
    """最強形式 baseline（驗收標準 #3 的正確語意）：`duration_ms` 欄位的**存在與否**
    不得改變 token_usage 聚合的任何一個欄位。

    做法：同一批事件跑兩次——(a) 每筆 payload 帶 duration_ms（新程式產出的形狀）、
    (b) 同批剝掉 duration_ms（改動前舊程式產出的形狀），兩本 dict 必須完全相等。
    這才等價於「混入舊事件時 token_usage 聚合與改動前完全一致」——改動前的程式
    根本看不到 duration_ms，故新欄位必須對 token 聚合完全透明。

    第 1 版此測試錯誤地比較「全新批」vs「全新批＋額外舊事件」——舊事件本身仍是合法
    token_usage 事件，本來就該計入 calls/tokens，兩批當然不等；該版語意已廢棄
    （與主檔 test_legacy_events_... 的 calls=4 全計語意矛盾）。
    """
    base = [
        _tu_event(
            "openai",
            "gpt-5",
            "engineer",
            duration_ms=400,
            prompt_tokens=100,
            completion_tokens=20,
            total_tokens=120,
        ),
        _tu_event(
            "claude",
            "claude-opus",
            "pm",
            duration_ms=200,
            prompt_tokens=50,
            completion_tokens=10,
            total_tokens=60,
        ),
        _tu_event(
            "openai",
            "gpt-5",
            "qa",
            duration_ms=150,
            prompt_tokens=30,
            completion_tokens=5,
            total_tokens=35,
        ),
    ]
    # 剝掉 duration_ms＝改動前舊事件形狀（其餘 payload 完全相同）
    stripped = []
    for ev in base:
        p = {k: v for k, v in ev["payload"].items() if k != "duration_ms"}
        stripped.append({**ev, "payload": p})
    assert all("duration_ms" not in ev["payload"] for ev in stripped)

    tu_with = history._derive_token_usage(base)
    tu_without = history._derive_token_usage(stripped)

    # 整本 dict 必須完全相等（不是「部分欄位 equal」這種弱斷言）
    assert tu_with == tu_without, (
        f"duration_ms 欄位影響了 token_usage 聚合（應完全透明）：\n"
        f"with={tu_with!r}\nwithout={tu_without!r}"
    )


def test_legacy_events_zero_impact_on_latency_aggregation_orthogonal():
    """正交性：同一批事件 → latency 與 token_usage 是兩個獨立的計算路徑，
    互不干擾。具體：把 legacy 事件「真的會被 token_usage 看到，但不進 latency」這件事
    以 dict diff 證明。
    """
    evs = [
        _tu_event("openai", "gpt-5", "engineer", duration_ms=400),
        _tu_event("openai", "gpt-5", "engineer", duration_ms=None),  # 舊事件
    ]
    tu = history._derive_token_usage(evs)
    lat = history._derive_latency(evs)

    # token_usage 把舊事件算進 calls（2 筆）
    assert tu["total"]["calls"] == 2
    # latency 把舊事件跳過（1 筆）
    assert lat["total"]["count"] == 1
    assert lat["total"]["sum_ms"] == 400


# C) --- 跨場合併 sum/count 安全性 ----------------------------------------------


def test_latency_cross_session_merge_is_sum_count_safe():
    """Architect 強調的「可合併聚合」合約（逐維度逐 key）：

      對每個維度（total / by_provider / by_model / by_role）的每個 key，
      merge(la, lb)[key] = {count: a+b, sum_ms: a+b, max_ms: max(a,b)}
      必須等於「兩場事件合成一場」跑 _derive_latency 的結果；avg_ms 由合併後
      sum//count 重導。若 meta 只存 avg 而非 sum，這條會壞。

    退回第 4 點定性結論：第 1 版失敗是**測試 bug、非實作 bug**——它把 A、B 全部桶的
    (count,sum) 做匿名笛卡兒積當期望值，等於要求「A.total + B.by_provider.openai」這種
    跨維度無意義組合也出現在合併結果，必然 missing。_derive_latency 是逐事件累加，
    合併語意天然逐 key 成立，本測試以正確的 key-wise 斷言證明之。
    """
    # 故意設計 key 有交集（openai/gpt-5/engineer 兩場都有）也有獨佔（claude、antigravity）
    evs_a = [
        _tu_event("openai", "gpt-5", "engineer", duration_ms=100),
        _tu_event("claude", "claude-opus", "pm", duration_ms=200),
    ]
    evs_b = [
        _tu_event("openai", "gpt-5", "engineer", duration_ms=300),  # 跨場 bucket 合併用同一 key
        _tu_event("antigravity", "agy-flash", "engineer", duration_ms=400),
    ]
    combined = evs_a + evs_b

    la = history._derive_latency(evs_a)
    lb = history._derive_latency(evs_b)
    lc = history._derive_latency(combined)

    def _merge_bucket(a: dict | None, b: dict | None) -> dict:
        """/api/metrics 未來跨場合併的參考實作：sum/count 相加、max 取大、avg 重導。"""
        blank = {"count": 0, "sum_ms": 0, "max_ms": 0}
        a = a or blank
        b = b or blank
        count = a["count"] + b["count"]
        sum_ms = a["sum_ms"] + b["sum_ms"]
        return {
            "count": count,
            "sum_ms": sum_ms,
            "max_ms": max(a["max_ms"], b["max_ms"]),
            "avg_ms": (sum_ms // count) if count else 0,
        }

    # total 桶：合併後與「合成一場」一致
    assert _merge_bucket(la["total"], lb["total"]) == lc["total"]

    # by_* 三維度：逐 key（含只出現在單場的 key）合併後與「合成一場」一致
    for dim in ("by_provider", "by_model", "by_role"):
        all_keys = set(la[dim]) | set(lb[dim])
        assert all_keys == set(lc[dim]), (
            f"{dim} 合併後 key 集合不一致：expected={all_keys}, got={set(lc[dim])}"
        )
        for key in all_keys:
            merged = _merge_bucket(la[dim].get(key), lb[dim].get(key))
            assert merged == lc[dim][key], (
                f"{dim}[{key}] 跨場合併失真：merged={merged}, combined={lc[dim][key]}"
            )


def test_latency_avg_ms_rederivable_across_sessions():
    """avg_ms 由 sum/count 重建：跨場合併後 avg 必須等於「全部事件當一場跑」的 avg。

    數字故意挑選 1300/3 不整除，驗 floor 整除的隱性合約語意一致。
    """
    evs_a = [_tu_event("openai", "gpt-5", "engineer", duration_ms=500)]
    evs_b = [
        _tu_event("openai", "gpt-5", "engineer", duration_ms=400),
        _tu_event("openai", "gpt-5", "engineer", duration_ms=400),
    ]
    combined = evs_a + evs_b

    la = history._derive_latency(evs_a)
    lb = history._derive_latency(evs_b)
    lc = history._derive_latency(combined)

    # 重導平均：(sum_a + sum_b) // (count_a + count_b)
    expected_avg = (la["total"]["sum_ms"] + lb["total"]["sum_ms"]) // (
        la["total"]["count"] + lb["total"]["count"]
    )
    assert lc["total"]["avg_ms"] == expected_avg
    # 1300 / 3 = 433（不是 433.33 四捨五入成 433；剛好等於 floor）
    assert lc["total"]["avg_ms"] == 433, "順手鎖死整數除法語意"


# D) --- acceptance #1 字面 grep 落地 -------------------------------------------


def test_events_py_source_contains_duration_ms_keyword():
    """acceptance #1 字面：grep `duration_ms` 可在 events.py 命中。
    掃原始檔（不是 to_dict 結果），防止有人把參數搬到非預期位置（payload 內層 dict 等）。
    """
    events_path = Path(__file__).resolve().parents[1] / "studio" / "events.py"
    src = events_path.read_text(encoding="utf-8")
    # 至少三處：參數宣告、None 判斷、payload 寫入
    assert re.search(r"^\s*duration_ms:\s*int\s*\|\s*None\s*=\s*None", src, re.MULTILINE), (
        "events.py 簽名應含 `duration_ms: int | None = None`"
    )
    assert "if duration_ms is not None:" in src, "events.py 應以「非 None 才寫入 payload」守門"
    assert 'payload["duration_ms"]' in src, "events.py 應寫入 payload['duration_ms']"


def test_providers_py_docstring_documents_full_loop_semantic():
    """acceptance #4：非 Claude 路徑（providers.py）的 docstring/註解必須
    明示「整輪」語意，且說明與 Claude 端 duration_api_ms（單次 API 通訊）不同。
    """
    providers_path = Path(__file__).resolve().parents[1] / "studio" / "providers.py"
    src = providers_path.read_text(encoding="utf-8")

    # 「整輪工具迴圈」語意字面必須在原始碼註解/docstring 出現
    assert "整輪" in src and "工具迴圈" in src, "providers.py 必須在註解/字串中標註「整輪工具迴圈」"
    # 必須明示與 Claude 端 duration_api_ms 不同（單次 API 通訊）
    assert "duration_api_ms" in src, (
        "providers.py 必須在某處明示與 Claude 端的 duration_api_ms 對照"
    )
    # perf_counter 整輪計時的入口必須真的用 time.perf_counter（已被 grep 證實），
    # 額外守護：截斷採 int() 而非 round
    assert "int((time.perf_counter() - loop_started_at) * 1000)" in src, (
        "providers.py 必須用 int() 截斷（不四捨五入）"
    )


# E) --- finish_session 真實序列化路徑下「有 duration 必計入」 ----------------


def test_finish_session_latency_counts_event_with_duration_ms(monkeypatch, tmp_path):
    """離線 e2e 等級：真實 start → record → finish 路徑，跑 fake 專家（無 duration），
    但『手動插入』一筆含 duration_ms 的 token_usage 事件到 jsonl 中，
    驗 finish_session 序列化後的 meta.latency.total.count >= 1。
    """
    sid = "lat-finish-positive"
    history.start_session(sid, "需求：含 duration")

    # 三筆無 duration 舊事件 + 一筆有 duration 新事件
    history.record_event(sid, _tu_event("openai", "gpt-5", "engineer", duration_ms=None))
    history.record_event(sid, _tu_event("claude", "claude-opus", "pm", duration_ms=None))
    history.record_event(sid, _tu_event("openai", "gpt-5", "qa", duration_ms=None))
    history.record_event(sid, _tu_event("openai", "gpt-5", "engineer", duration_ms=789))
    history.record_event(
        sid, {"type": "done", "session_id": sid, "ts": 0, "payload": {"completed": True}}
    )

    meta = history.finish_session(sid)
    assert meta is not None
    # token_usage calls=4（舊 3 + 新 1），latency count=1（只看新）
    assert meta["token_usage"]["total"]["calls"] == 4
    assert meta["latency"]["total"]["count"] == 1
    assert meta["latency"]["total"]["sum_ms"] == 789
    assert meta["latency"]["total"]["max_ms"] == 789
    assert meta["latency"]["total"]["avg_ms"] == 789
    # by_provider.openai 出現；claude 因舊事件沒 duration 故不應出現在 latency
    assert "openai" in meta["latency"]["by_provider"]
    assert "claude" not in meta["latency"]["by_provider"]


def test_finish_session_latency_keys_unaltered_by_mixed_events(monkeypatch, tmp_path):
    """進階正交性：同一 meta 內，latency 與 token_usage 各自的 by_provider/by_model/by_role
    集合在「混入舊事件」時不能互相污染。例如：舊事件 openai×1 + claude×1 不該讓 latency
    的 by_provider 出現這兩個 key（因 duration 缺失），但 token_usage 兩者皆該出現。
    """
    sid = "ortho"
    history.start_session(sid, "正交性測試")

    history.record_event(sid, _tu_event("openai", "gpt-5", "engineer", duration_ms=100))
    history.record_event(sid, _tu_event("openai", "gpt-5", "engineer", duration_ms=None))  # 舊
    history.record_event(
        sid, _tu_event("claude", "claude-opus", "engineer", duration_ms=None)
    )  # 舊
    history.record_event(sid, _tu_event("antigravity", "agy-flash", "qa", duration_ms=300))

    history.record_event(
        sid, {"type": "done", "session_id": sid, "ts": 0, "payload": {"completed": True}}
    )
    meta = history.finish_session(sid)

    # token_usage by_provider 三 provider 皆出現（calls 各 1, 1, 1）
    assert set(meta["token_usage"]["by_provider"]) == {"openai", "claude", "antigravity"}
    # latency by_provider 只有 2 個（openai + antigravity；claude 缺 duration 不入）
    assert set(meta["latency"]["by_provider"]) == {"openai", "antigravity"}
    # latency by_role.engineer = 1 筆（100ms）；by_role.qa = 1 筆（300ms）
    assert meta["latency"]["by_role"]["engineer"]["count"] == 1
    assert meta["latency"]["by_role"]["engineer"]["sum_ms"] == 100
    assert meta["latency"]["by_role"]["qa"]["count"] == 1
    assert meta["latency"]["by_role"]["qa"]["sum_ms"] == 300


# F) --- events / experts 的雙重 payload 形狀細節 -----------------------------


def test_events_token_usage_payload_keeps_duration_ms_int_not_string():
    """邊界：duration_ms 傳入時即使上游型別怪，payload 必須保持 int（已 _derive_latency 的
    _int_ms 雖會截斷回 int，但讀取端若把 payload 直接餵給下游序列化，型別應一致）。
    """
    ev = events.token_usage("s", "engineer", "openai", "gpt-5", 10, 5, 15, duration_ms=1500)
    assert ev.to_dict()["payload"]["duration_ms"] == 1500
    assert isinstance(ev.to_dict()["payload"]["duration_ms"], int)


def test_events_token_usage_passing_negative_or_string_duration_still_int_or_missing():
    """回溯攻擊面：有人誤傳負數或字串——目前的 events.token_usage 不做轉型，
    會原樣塞 payload；下游 _int_ms 已在 history._derive_latency 內做 max(0, int(value or 0))
    防禦。驗證這層防線存在（斷言不能讓 _derive_latency 直接炸）。
    """
    bad_evs = [
        {
            "type": "token_usage",
            "session_id": "s",
            "ts": 0,
            "payload": {
                "speaker": "engineer",
                "provider": "openai",
                "model": "gpt-5",
                "duration_ms": -50,  # 負數
            },
        },
        {
            "type": "token_usage",
            "session_id": "s",
            "ts": 0,
            "payload": {
                "speaker": "engineer",
                "provider": "openai",
                "model": "gpt-5",
                "duration_ms": "abc",  # 字串（_int_ms 應回傳 0、不丟例外）
            },
        },
    ]
    lat = history._derive_latency(bad_evs)
    # 兩筆都被「算進」但有效值都被 _int_ms 歸 0：count=2, sum/max=0
    assert lat["total"]["count"] == 2
    assert lat["total"]["sum_ms"] == 0
    assert lat["total"]["max_ms"] == 0
    assert lat["total"]["avg_ms"] == 0
