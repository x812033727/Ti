"""flow 額度感知派工純函式的單元測試：parse_dispatch 各格式與 choose_dispatch 全規則。

全部用合成 digest／allowed_models，不打外網、不依賴 provider_quota（維持 flow 純函式邊界）。
"""

from __future__ import annotations

from studio import flow

# --- parse_dispatch ----------------------------------------------------------


def test_parse_dispatch_basic():
    out = flow.parse_dispatch("任務: #1 甲\n派工: #1 codex\n任務: #2 乙")
    assert out == {1: {"provider": "codex", "model": ""}}


def test_parse_dispatch_with_model():
    out = flow.parse_dispatch("派工: #2 claude claude-haiku-4-5")
    assert out == {2: {"provider": "claude", "model": "claude-haiku-4-5"}}


def test_parse_dispatch_model_with_spaces():
    # Antigravity 的模型是含空白的顯示名稱，model 必須收整段尾巴。
    out = flow.parse_dispatch("派工: #3 antigravity Gemini 3.5 Flash (Low)")
    assert out == {3: {"provider": "antigravity", "model": "Gemini 3.5 Flash (Low)"}}


def test_parse_dispatch_fullwidth_colon_and_case():
    out = flow.parse_dispatch("派工： #4 Codex")
    assert out == {4: {"provider": "codex", "model": ""}}  # 全形冒號容錯、provider 正規化小寫


def test_parse_dispatch_last_line_wins_and_multi():
    text = "派工: #1 codex\n中間其他文字\n派工: #2 minimax\n派工: #1 claude"
    out = flow.parse_dispatch(text)
    assert out[1] == {"provider": "claude", "model": ""}  # 同 id 取最後一行
    assert out[2] == {"provider": "minimax", "model": ""}


def test_parse_dispatch_none_or_empty():
    assert flow.parse_dispatch("") == {}
    assert flow.parse_dispatch(None) == {}
    assert flow.parse_dispatch("任務: #1 甲\n沒有派工行") == {}


# --- choose_dispatch ---------------------------------------------------------


def _digest(**providers):
    """合成 digest：值可為 float（用量%）、"down"（未就緒）、"error"（查詢異常）。"""
    out = {}
    for key, val in providers.items():
        if val == "down":
            out[key] = {"ready": False, "error": None, "max_used": None, "soonest_reset": None}
        elif val == "error":
            out[key] = {
                "ready": True,
                "error": "unauthorized",
                "max_used": None,
                "soonest_reset": None,
            }
        else:
            out[key] = {"ready": True, "error": None, "max_used": float(val), "soonest_reset": None}
    return out


_MODELS = {
    "claude": ("claude-fable-5", "claude-haiku-4-5"),
    "codex": ("gpt-5.5",),
    "antigravity": ("Gemini 3.5 Flash (Low)",),
}


def test_choose_hint_adopted_when_unconstrained():
    dig = _digest(claude=30, codex=50)
    out = flow.choose_dispatch(dig, {"id": 1}, {"provider": "codex", "model": ""}, _MODELS, [])
    assert out["provider"] == "codex"
    assert out["model"] == ""
    assert out["reason"]  # reason 為非空繁中一句話


def test_choose_hint_constrained_falls_to_lowest_usage():
    dig = _digest(claude=30, codex=95, minimax=60)
    out = flow.choose_dispatch(dig, {"id": 1}, {"provider": "codex"}, _MODELS, [])
    assert out["provider"] == "claude"  # codex 用量 95% ≥ 門檻 → 跳開，取最低者


def test_choose_hint_unknown_provider_ignored():
    dig = _digest(claude=30, codex=10)
    out = flow.choose_dispatch(dig, {"id": 1}, {"provider": "openai"}, _MODELS, [])
    assert out["provider"] == "codex"  # hint 不在 digest → 忽略，走用量最低


def test_choose_spreads_by_lowest_usage():
    dig = _digest(claude=80, codex=10, minimax=40)
    out = flow.choose_dispatch(dig, {"id": 1}, {}, _MODELS, [])
    assert out["provider"] == "codex"


def test_choose_tie_avoids_recent_tail():
    dig = _digest(claude=20, codex=20)
    # 同用量：剛派過 claude → 這次避開、輪到 codex（分攤額度）。
    out = flow.choose_dispatch(dig, {"id": 2}, {}, _MODELS, ["claude"])
    assert out["provider"] == "codex"
    # 剛派過 codex → 輪回 claude。
    out2 = flow.choose_dispatch(dig, {"id": 3}, {}, _MODELS, ["claude", "codex"])
    assert out2["provider"] == "claude"


def test_choose_tie_prefers_performance_before_recent():
    dig = _digest(claude=20, codex=20)
    # 同用量時 performance 是第一個次序鍵：codex 表現較高 → 即使剛用過 codex 仍選它。
    out = flow.choose_dispatch(
        dig, {"id": 1}, {}, _MODELS, ["codex"], performance={"claude": 50.0, "codex": 80.0}
    )
    assert out["provider"] == "codex"
    # performance 缺省視為 0：只有 claude 有分數 → 選 claude。
    out2 = flow.choose_dispatch(dig, {"id": 1}, {}, _MODELS, [], performance={"claude": 1.0})
    assert out2["provider"] == "claude"


def test_choose_model_whitelist():
    dig = _digest(claude=10)
    # hint.model 在該 provider 白名單 → 採用。
    ok = flow.choose_dispatch(
        dig, {"id": 1}, {"provider": "claude", "model": "claude-haiku-4-5"}, _MODELS, []
    )
    assert ok["model"] == "claude-haiku-4-5"
    # 不在白名單（LLM 即興發明）→ 空字串＝沿用該 provider 預設模型槽。
    bad = flow.choose_dispatch(
        dig, {"id": 1}, {"provider": "claude", "model": "claude-imaginary-9"}, _MODELS, []
    )
    assert bad["model"] == ""


def test_choose_model_checked_against_chosen_provider():
    # hint 指 codex（受限）→ 改派 claude；hint.model 是 codex 的模型、不在 claude 白名單 → 棄用。
    dig = _digest(claude=10, codex=95)
    out = flow.choose_dispatch(
        dig, {"id": 1}, {"provider": "codex", "model": "gpt-5.5"}, _MODELS, []
    )
    assert out["provider"] == "claude" and out["model"] == ""


def test_choose_all_constrained_takes_lowest_ready():
    dig = _digest(claude=95, codex=99, minimax="down")
    out = flow.choose_dispatch(dig, {"id": 1}, {}, _MODELS, [])
    assert out["provider"] == "claude"  # 全受限 → 就緒中用量最低者


def test_choose_error_entries_excluded():
    dig = _digest(claude="error", codex=40)
    out = flow.choose_dispatch(dig, {"id": 1}, {}, _MODELS, [])
    assert out["provider"] == "codex"  # 查詢異常視同不可用


def test_choose_all_down_returns_empty():
    dig = _digest(claude="down", codex="error")
    out = flow.choose_dispatch(dig, {"id": 1}, {}, _MODELS, [])
    assert out == {"provider": "", "model": "", "reason": out["reason"]}
    assert out["provider"] == "" and out["model"] == "" and out["reason"]


def test_choose_empty_digest_returns_empty():
    out = flow.choose_dispatch({}, {"id": 1}, {}, _MODELS, [])
    assert out["provider"] == "" and out["model"] == ""


# --- choose_dispatch：auto 派工模式（model_free＋兩家子集＋門檻 95）------------


def test_choose_model_free_passes_arbitrary_model():
    # auto 派工：hint 就緒未受限 → 任意模型 ID 原樣直通，不查白名單。
    dig = _digest(claude=10, codex=30)
    out = flow.choose_dispatch(
        dig,
        {"id": 1},
        {"provider": "claude", "model": "claude-brand-new-6"},
        _MODELS,
        [],
        model_free=True,
    )
    assert out["provider"] == "claude" and out["model"] == "claude-brand-new-6"


def test_choose_model_free_dropped_when_provider_rebound():
    # auto 派工但 hint 家受限被改派 → 模型丟空（A 家模型 ID 不直通 B 家），改用該家預設槽。
    dig = _digest(claude=10, codex=96)
    out = flow.choose_dispatch(
        dig,
        {"id": 1},
        {"provider": "codex", "model": "gpt-custom-x"},
        _MODELS,
        [],
        threshold=95.0,
        model_free=True,
    )
    assert out["provider"] == "claude" and out["model"] == ""


def test_choose_threshold_95_boundary():
    # 門檻 95：94.9% 照派、95.0% 改派（達門檻即受限）。
    dig_ok = _digest(claude=94.9, codex=10)
    ok = flow.choose_dispatch(
        dig_ok, {"id": 1}, {"provider": "claude"}, _MODELS, [], threshold=95.0
    )
    assert ok["provider"] == "claude"
    dig_over = _digest(claude=95.0, codex=10)
    over = flow.choose_dispatch(
        dig_over, {"id": 1}, {"provider": "claude"}, _MODELS, [], threshold=95.0
    )
    assert over["provider"] == "codex"


def test_choose_subset_digest_clamps_offlimit_hint():
    # auto 派工的兩家子集 digest：PM 違規指定 minimax（不在 digest）→ 落到子集中用量最低者。
    dig = _digest(claude=40, codex=20)
    out = flow.choose_dispatch(
        dig, {"id": 1}, {"provider": "minimax", "model": "MiniMax-M2"}, _MODELS, [], model_free=True
    )
    assert out["provider"] == "codex" and out["model"] == ""


# --- parse_next_step 的 `模型:` 行 --------------------------------------------


def test_parse_next_step_model_line():
    out = flow.parse_next_step("下一步: architect\n指示: 複核\nprovider: codex\n模型: gpt-5.5")
    assert out["provider"] == "codex" and out["model"] == "gpt-5.5"
    # 全形冒號、含空白的模型名、取最後一行。
    out2 = flow.parse_next_step("模型：Gemini 3.5 Flash (Low)\n模型: claude-haiku-4-5")
    assert out2["model"] == "claude-haiku-4-5"


def test_parse_next_step_model_default_empty():
    assert flow.parse_next_step("下一步: engineer")["model"] == ""
