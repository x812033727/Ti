"""冒煙腳本 `scripts/smoke_llm.py` 量測邏輯單元測試（任務 #5）。

覆蓋驗收 #3／#5 的量測核心，全程離線、零 LLM、不連 `api.anthropic.com`：

- ``measure_mention_adherence``：遵循率定義與開場排除（round_robin 只排首輪首位、
  parallel 排整個首輪），分母正確。
- ``classify_consensus``：三態判別——有反對／強共識／「全員無反對但無明確同意」弱訊號，
  **不把弱訊號誤判為強共識**（守驗收 #3）。
- ``count_failure_fallbacks``：429 與 SDK 錯誤文字為**兩條獨立 counter**，且錨定
  ``【系統】`` 前綴，專家原文引用同字樣不被誤計。
- 端到端 offline 兩模式各跑一次零崩潰；``--dissent`` 確實驅動「有反對」劇本。

429／SDK 錯誤文字防線本體（experts.py）的單元測試見 ``test_experts_ratelimit.py``，
本檔聚焦冒煙腳本作為**純消費端**的量測正確性，不重複防線測試。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from studio.discussion import DiscussionResult, Mention, Utterance
from studio.experts import API_ERROR_FALLBACK_MARKER, RATE_LIMIT_FALLBACK_MARKER

# scripts/ 非套件，用 spec 直接載入腳本模組。
_SMOKE_PATH = Path(__file__).resolve().parent.parent / "scripts" / "smoke_llm.py"
_spec = importlib.util.spec_from_file_location("smoke_llm", _SMOKE_PATH)
smoke = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(smoke)


# --- 小工具：拼裝假 transcript / result ---------------------------------


def _utt(
    round_no: int, speaker: str, text: str, mentions: list[Mention] | None = None
) -> Utterance:
    return Utterance(round=round_no, speaker=speaker, text=text, mentions=mentions or [])


def _result(transcript: list[Utterance], summary: dict | None = None) -> DiscussionResult:
    return DiscussionResult(transcript=transcript, stop_reason="max_rounds", summary=summary or {})


def _m(speaker: str, target: str, stance: str = "同意") -> Mention:
    return Mention(speaker=speaker, target=target, stance=stance)


# --- classify_consensus：三態判別，弱訊號不誤判為強共識 -------------------


def test_consensus_有反對():
    out = smoke.classify_consensus({"consensus": ["A 同意 B"], "disagreements": ["C 反對 D"]})
    assert out["label"] == "有反對"
    assert out["no_dissent"] is False
    assert out["is_strong_consensus"] is False


def test_consensus_強共識():
    out = smoke.classify_consensus({"consensus": ["A 同意 B"], "disagreements": []})
    assert out["no_dissent"] is True
    assert out["has_explicit_agreement"] is True
    assert out["is_strong_consensus"] is True


def test_consensus_全員無反對但無明確同意_不可誤判強共識():
    """守驗收 #3：無反對但無人明確同意，只是弱訊號，不可當強共識。"""
    out = smoke.classify_consensus({"consensus": [], "disagreements": []})
    assert out["no_dissent"] is True
    assert out["has_explicit_agreement"] is False
    assert out["is_strong_consensus"] is False
    assert "不可誤判" in out["label"]


def test_consensus_容忍缺鍵():
    out = smoke.classify_consensus({})
    assert out["is_strong_consensus"] is False


# --- _is_opener：開場判定（round_robin vs parallel） ---------------------


def test_is_opener_round_robin_僅首輪首位():
    transcript = [_utt(1, "甲", "x"), _utt(1, "乙", "y"), _utt(2, "甲", "z")]
    first = smoke._round1_first_speaker(transcript)
    assert first == "甲"
    assert smoke._is_opener("round_robin", transcript[0], first) is True  # 首輪首位
    assert smoke._is_opener("round_robin", transcript[1], first) is False  # 首輪非首位可見前者
    assert smoke._is_opener("round_robin", transcript[2], first) is False  # 第二輪


def test_is_opener_parallel_整個首輪():
    transcript = [_utt(1, "甲", "x"), _utt(1, "乙", "y"), _utt(2, "甲", "z")]
    first = smoke._round1_first_speaker(transcript)
    assert smoke._is_opener("parallel", transcript[0], first) is True
    assert smoke._is_opener("parallel", transcript[1], first) is True  # parallel 首輪全排除
    assert smoke._is_opener("parallel", transcript[2], first) is False


# --- measure_mention_adherence：遵循率與分母 -----------------------------


def test_adherence_round_robin_排除首輪首位():
    # 首輪首位無 mention（開場排除）；其餘 3 筆均有合法 mention → 100%
    transcript = [
        _utt(1, "甲", "開場", []),
        _utt(1, "乙", "回應", [_m("乙", "甲")]),
        _utt(2, "甲", "回應", [_m("甲", "乙")]),
        _utt(2, "乙", "回應", [_m("乙", "甲")]),
    ]
    out = smoke.measure_mention_adherence(_result(transcript), "round_robin")
    assert out["excluded_openers"] == 1
    assert out["eligible_total"] == 3
    assert out["compliant_total"] == 3
    assert out["overall_rate"] == 1.0


def test_adherence_部分不遵循():
    # 首輪首位排除；剩 3 筆中 1 筆無 mention → 2/3
    transcript = [
        _utt(1, "甲", "開場", []),
        _utt(1, "乙", "離題無引用", []),
        _utt(2, "甲", "回應", [_m("甲", "乙")]),
        _utt(2, "乙", "回應", [_m("乙", "甲")]),
    ]
    out = smoke.measure_mention_adherence(_result(transcript), "round_robin")
    assert out["eligible_total"] == 3
    assert out["compliant_total"] == 2
    assert out["overall_rate"] == pytest.approx(2 / 3)


def test_adherence_parallel_首輪全排除():
    transcript = [
        _utt(1, "甲", "開場", []),
        _utt(1, "乙", "開場", []),
        _utt(2, "甲", "回應", [_m("甲", "乙")]),
        _utt(2, "乙", "回應", [_m("乙", "甲")]),
    ]
    out = smoke.measure_mention_adherence(_result(transcript), "parallel")
    assert out["excluded_openers"] == 2
    assert out["eligible_total"] == 2
    assert out["overall_rate"] == 1.0


def test_adherence_全開場時分母為零():
    transcript = [_utt(1, "甲", "開場", []), _utt(1, "乙", "開場", [])]
    out = smoke.measure_mention_adherence(_result(transcript), "parallel")
    assert out["eligible_total"] == 0
    assert out["overall_rate"] is None  # _fmt_rate 會標 N/A


# --- count_failure_fallbacks：兩條獨立 counter + 前綴錨定 ----------------


def test_failure_counts_兩條獨立():
    transcript = [
        _utt(1, "甲", f"【系統】發言{RATE_LIMIT_FALLBACK_MARKER}退避重試 3 次仍失敗，本輪中止。"),
        _utt(1, "乙", f"【系統】{API_ERROR_FALLBACK_MARKER}，本輪中止。"),
        _utt(2, "甲", "正常發言"),
    ]
    out = smoke.count_failure_fallbacks(_result(transcript))
    assert out["rate_limit_hits"] == 1
    assert out["api_error_hits"] == 1


def test_failure_counts_專家原文引用不被誤計():
    """反向黑樣本：發言原文含 marker 字樣但非【系統】前綴 → 不計（真實面假綠歸零）。"""
    transcript = [
        _utt(1, "甲", f"回應 @乙: 同意。我們上次{RATE_LIMIT_FALLBACK_MARKER}才崩潰，要補退避。"),
        _utt(1, "乙", f"提醒大家 {API_ERROR_FALLBACK_MARKER} 時要走 fallback。"),
    ]
    out = smoke.count_failure_fallbacks(_result(transcript))
    assert out["rate_limit_hits"] == 0
    assert out["api_error_hits"] == 0


# --- 端到端 offline：兩模式零崩潰 + dissent 驅動「有反對」 ----------------


async def test_offline_round_robin_零崩潰且強共識():
    result = await smoke.run_smoke(
        mode="round_robin", concurrency=2, rounds=2, offline=True, dissent=False
    )
    assert result.transcript  # 有發言
    run = smoke.collect_run("round_robin", 2, 2, True, False, result)
    assert run["consensus"]["no_dissent"] is True
    assert run["failures"] == {"rate_limit_hits": 0, "api_error_hits": 0}


async def test_offline_parallel_零崩潰():
    result = await smoke.run_smoke(
        mode="parallel", concurrency=2, rounds=2, offline=True, dissent=False
    )
    assert result.transcript
    adh = smoke.measure_mention_adherence(result, "parallel")
    # parallel 首輪全為開場，被排除
    assert adh["excluded_openers"] == len([u for u in result.transcript if u.round == 1])


async def test_offline_dissent_驅動有反對劇本():
    result = await smoke.run_smoke(
        mode="round_robin", concurrency=2, rounds=2, offline=True, dissent=True
    )
    out = smoke.classify_consensus(result.summary)
    assert out["no_dissent"] is False
    assert out["label"] == "有反對"


# --- 報告渲染：offline 四段齊全 + 移交待辦段 ------------------------------


def test_render_report_offline_四段齊全():
    transcript = [
        _utt(1, "甲", "開場", []),
        _utt(1, "乙", "回應", [_m("乙", "甲")]),
    ]
    run = smoke.collect_run("round_robin", 2, 2, True, False, _result(transcript))
    report = smoke.render_report([run], offline=True)
    assert "一、發言品質抽樣" in report
    assert "二、@引用遵循率" in report
    assert "三、rate limit" in report
    assert "四、SDK 錯誤文字命中數" in report
    assert "未涵蓋真實 API 面" in report  # 驗收 #6 移交待辦
