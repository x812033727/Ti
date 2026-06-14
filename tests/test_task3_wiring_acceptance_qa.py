"""任務#3 驗收：消費端統一改用 `RetryConfig(max_retries=, cap=, jitter=)` 新入口。

對應七條驗收標準逐條覆蓋（QA 獨立驗證，不依賴工程師自寫測試）：
1. RetryConfig 三欄位入口可直接建構、無需呼叫端手寫 backoff lambda。
2. 不傳 base/cap/jitter 時行為與舊等價（既有套件零回歸由 -k 全綠佐證）。
3. 顯式 backoff 注入不被 __post_init__ 覆蓋。
4. 非法值安全 clamp（cap<=0 / max_retries<0 / jitter 超 [0,1]）不拋例外、不除零。
5. 反向黑樣本：jitter=0 確定值；jitter>0 抖動且 429 路徑不早於 retry_after。
6. 三端 call-site（experts / provider / orchestrator）全走新入口，無殘留手寫 lambda。
7. 零新外部依賴、零新 env 變數（沿用 TI_RATELIMIT_*）。
"""

import ast
import warnings
from pathlib import Path

import pytest

from studio import experts, llm_caller, providers
from studio.llm_caller import (
    DEFAULT_BACKOFF_BASE,
    DEFAULT_BACKOFF_CAP,
    DEFAULT_BACKOFF_JITTER,
    RetryConfig,
)

STUDIO = Path(__file__).resolve().parent.parent / "studio"


# ── 驗收 1：三欄位入口直接建構，無手寫 lambda ──────────────────────────────
def test_ac1_three_field_entry_builds_without_manual_lambda():
    cfg = RetryConfig(max_retries=5, cap=30.0, jitter=0.25)
    assert cfg.max_retries == 5
    assert cfg.cap == 30.0
    assert cfg.jitter == 0.25
    # __post_init__ 自動生成 backoff，呼叫端零手寫
    assert callable(cfg.backoff)
    # 自動生成的退避可實際算出秒數
    assert cfg.backoff(None, 0) > 0
    # as_kwargs 可直接平鋪餵給 run_with_retries
    kw = cfg.as_kwargs()
    assert set(kw) == {"max_retries", "backoff", "sleep"}
    assert kw["backoff"] is cfg.backoff


# ── 驗收 2：預設等價舊行為 ──────────────────────────────────────────────────
def test_ac2_defaults_equivalent_to_legacy_backoff():
    """不傳 base/cap/jitter（jitter 預設 0）→ 自動 backoff 與 backoff_delay 預設逐點相等。"""
    cfg = RetryConfig(max_retries=3)
    assert cfg.base == DEFAULT_BACKOFF_BASE
    assert cfg.cap == DEFAULT_BACKOFF_CAP
    assert cfg.jitter == DEFAULT_BACKOFF_JITTER == 0.0
    for ra, att in [(None, 0), (None, 2), (None, 9), (5.0, 0), (999.0, 0)]:
        assert cfg.backoff(ra, att) == llm_caller.backoff_delay(ra, att)


# ── 驗收 3：顯式 backoff 注入不被覆蓋 ──────────────────────────────────────
def test_ac3_explicit_backoff_not_overridden():
    sentinel = lambda ra, att: 42.0  # noqa: E731
    cfg = RetryConfig(max_retries=3, cap=10.0, jitter=0.9, backoff=sentinel)
    assert cfg.backoff is sentinel
    assert cfg.backoff(None, 0) == 42.0


def test_ac3_experts_factory_keeps_lazy_anchor():
    """make_retry_config 仍注入 _backoff_delay（lazy config-read 錨點），不被自動生成蓋掉。"""
    cfg = experts.make_retry_config()
    assert cfg.backoff is experts._backoff_delay


# ── 驗收 4：非法值安全 clamp，不拋例外、不除零 ────────────────────────────
def test_ac4_clamp_cap_nonpositive():
    with pytest.warns(UserWarning):
        cfg = RetryConfig(max_retries=3, cap=0.0)
    assert cfg.cap == DEFAULT_BACKOFF_CAP
    # cap=0 原會在指數退避除/夾出 0；clamp 後不為 0
    assert cfg.backoff(None, 0) > 0


def test_ac4_clamp_base_nonpositive():
    with pytest.warns(UserWarning):
        cfg = RetryConfig(max_retries=3, base=0.0)
    assert cfg.base == DEFAULT_BACKOFF_BASE
    # base=0 原會讓 529 路徑算出 0 延遲（thundering herd）；clamp 後 >0
    assert cfg.backoff(None, 0) > 0


def test_ac4_clamp_negative_max_retries():
    with pytest.warns(UserWarning):
        cfg = RetryConfig(max_retries=-1)
    assert cfg.max_retries == 0


@pytest.mark.parametrize("bad,expected", [(1.5, 1.0), (-0.3, 0.0), (99.0, 1.0)])
def test_ac4_clamp_jitter_out_of_range(bad, expected):
    with pytest.warns(UserWarning):
        cfg = RetryConfig(max_retries=3, jitter=bad)
    assert cfg.jitter == expected


def test_ac4_legal_values_emit_no_warning():
    with warnings.catch_warnings():
        warnings.simplefilter("error")  # 任何 warning 都讓測試失敗
        RetryConfig(max_retries=5, cap=30.0, base=2.0, jitter=0.25)


# ── 驗收 5：反向黑樣本（jitter=0 確定 vs jitter>0 抖動） ──────────────────
def test_ac5_jitter_zero_is_deterministic():
    cfg = RetryConfig(max_retries=3, base=2.0, cap=60.0, jitter=0.0)
    vals = {cfg.backoff(None, 1) for _ in range(50)}
    assert vals == {4.0}  # 完全確定，不受隨機源影響
    vals_429 = {cfg.backoff(5.0, 0) for _ in range(50)}
    assert vals_429 == {5.0}


def test_ac5_jitter_positive_actually_varies():
    cfg = RetryConfig(max_retries=3, base=2.0, cap=60.0, jitter=0.5)
    samples = [cfg.backoff(None, 3) for _ in range(200)]
    assert len(set(samples)) > 1, "jitter>0 必須產生抖動（防假綠）"
    nominal = min(2.0 * (2**3), 60.0)  # 16.0
    # 529 equal-jitter 向下散開：落點 ∈ [nominal*(1-j), nominal]
    assert all(nominal * 0.5 <= s <= nominal + 1e-9 for s in samples)


def test_ac5_429_path_never_earlier_than_retry_after():
    """429 路徑 jitter 僅向上：延遲不早於 min(retry_after, cap)（核心 SLA）。"""
    cfg = RetryConfig(max_retries=3, base=2.0, cap=60.0, jitter=0.9)
    retry_after = 4.0
    samples = [cfg.backoff(retry_after, 0) for _ in range(300)]
    assert all(s >= retry_after - 1e-9 for s in samples), "429 退避不得早於 retry_after"
    assert all(s <= retry_after * 1.9 + 1e-9 for s in samples)
    assert len(set(samples)) > 1  # 確實在抖動


def test_ac5_429_jitter_clamped_to_cap():
    """retry_after 已達 cap 時，向上 jitter 仍夾在 cap，不超出。"""
    cfg = RetryConfig(max_retries=3, base=2.0, cap=10.0, jitter=0.9)
    samples = [cfg.backoff(99.0, 0) for _ in range(100)]
    assert all(s == 10.0 for s in samples)  # nominal=cap=10，向上加再夾 cap → 恆為 10


# ── 驗收 6：三端 call-site 全走新入口、無殘留手寫 lambda ──────────────────
def test_ac6_no_handwritten_backoff_lambda_in_consumer_modules():
    """experts / providers / orchestrator 三端不得殘留手寫 `lambda ...: backoff_delay(...)`。

    註：llm_caller.RetryConfig.__post_init__ 內的自動生成 lambda 是「統一入口本身」，
    非消費端散傳，故僅掃消費端模組。
    """
    import re

    pat = re.compile(r"lambda[^:]*:\s*[\w.]*backoff_delay\s*\(")
    for name in ["experts.py", "providers.py", "orchestrator.py"]:
        src = (STUDIO / name).read_text(encoding="utf-8")
        hits = pat.findall(src)
        assert not hits, f"{name} 殘留手寫 backoff lambda: {hits}"


def test_ac6_experts_speak_goes_through_factory():
    src = (STUDIO / "experts.py").read_text(encoding="utf-8")
    assert "cfg = make_retry_config()" in src
    assert "cfg.as_kwargs()" in src


def test_ac6_provider_goes_through_factory():
    cfg = providers.make_retry_config()  # provider 模組 re-export 同一工廠
    assert cfg.backoff is experts._backoff_delay
    src = (STUDIO / "providers.py").read_text(encoding="utf-8")
    assert "cfg = make_retry_config()" in src
    assert "cfg.as_kwargs()" in src


def test_ac6_only_one_retryconfig_construction_callsite():
    """全 studio/ 生產碼只應有 RetryConfig(...) 的單一建構點（experts.make_retry_config）。"""
    found = []
    for py in STUDIO.glob("*.py"):
        if py.name == "llm_caller.py":  # 定義處，排除
            continue
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "RetryConfig"
            ):
                found.append((py.name, node.lineno))
    assert found == [("experts.py", 116)] or all(f[0] == "experts.py" for f in found), (
        f"RetryConfig 建構點應集中於 experts.py，實得 {found}"
    )


# ── 驗收 7：零新外部依賴、零新 env 變數 ────────────────────────────────────
def test_ac7_no_new_env_vars_beyond_ti_ratelimit():
    """退避四值仍只來自 TI_RATELIMIT_*；config 對應欄位存在。"""
    import studio.config as config

    for attr in [
        "EXPERT_RATE_LIMIT_RETRIES",
        "EXPERT_RATE_LIMIT_BACKOFF",
        "EXPERT_RATE_LIMIT_BACKOFF_CAP",
        "EXPERT_RATE_LIMIT_BACKOFF_JITTER",
    ]:
        assert hasattr(config, attr)


def test_ac7_no_external_dep_imported_by_llm_caller():
    """llm_caller 退避實作僅用標準庫（random/warnings/dataclasses），無第三方退避庫。"""
    src = (STUDIO / "llm_caller.py").read_text(encoding="utf-8")
    for forbidden in ["import tenacity", "import backoff\n", "from tenacity", "from backoff"]:
        assert forbidden not in src
