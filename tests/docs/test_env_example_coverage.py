"""`.env.example` 與 `studio/config.py` 的 TI_* 變數覆蓋守護（防設定文件漂移）。

契約：config.py 讀取的每個 `TI_*` 環境變數都必須出現在 .env.example（發現性——
運維不用翻原始碼才知道有哪些旋鈕）；反向亦守：.env.example 不得殘留 config.py
已不存在的變數（防打錯字/改名後殘留）。豁免僅限「動態鍵家族」（f-string 組出的
per-role 鍵，regex 天然掃不到、.env.example 已列代表範例）。

掃描範圍刻意只含 studio/config.py：它是設定 SSOT（CLAUDE.md 鐵則「設定走 config.py」），
已實測 studio/ 其他模組無私讀 TI_* env；若未來有人繞過 config 私讀，違反的是 SSOT 鐵則，
非本守護的職責。
"""

from __future__ import annotations

import re

from _repo import REPO_ROOT

CONFIG = REPO_ROOT / "studio" / "config.py"
ENV = REPO_ROOT / ".env.example"

# config.py 讀 env 的四種入口（os.getenv / _env_float / _env_int / env_bool）
_GETENV_RE = re.compile(
    r'(?:os\.getenv|_env_float|_env_int|env_bool)\(\s*[\'"](TI_[A-Z0-9_]+)[\'"]'
)
_ENV_VAR_RE = re.compile(r"TI_[A-Z0-9_]+")

# 動態鍵家族：config 以 f"TI_PROVIDER_{role}" / f"TI_MODEL_{role}" 等組鍵，regex 掃不到
# 全集；.env.example 以代表範例列出（TI_PROVIDER_ENGINEER/QA/PM、TI_MODEL_LEAD/FAST…），
# 反向檢查時以家族 regex 放行。
_DYNAMIC_FAMILY_RE = re.compile(
    r"TI_(?:PROVIDER|MODEL|OPENAI_MODEL|MINIMAX_MODEL|GEMINI_MODEL|CODEX_MODEL|"
    r"ANTIGRAVITY_MODEL)_[A-Z0-9_]+"
)

# 明確豁免（config 有讀但刻意不列入 .env.example 的變數）：每項附一行理由。
# 初始為空集——目前 config.py 的靜態 TI_* 變數已全數列入 .env.example。
EXEMPT: set[str] = set()


def _config_vars() -> set[str]:
    return set(_GETENV_RE.findall(CONFIG.read_text(encoding="utf-8")))


def _env_vars() -> set[str]:
    return set(_ENV_VAR_RE.findall(ENV.read_text(encoding="utf-8")))


def test_env_example_covers_all_config_vars():
    """config.py 的每個靜態 TI_* 變數都要出現在 .env.example（缺漏即漂移）。"""
    missing = sorted(_config_vars() - _env_vars() - EXEMPT)
    assert not missing, (
        f".env.example 缺 {len(missing)} 個 config.py 讀取的變數（請補上並附繁中一行說明）："
        f"{missing}"
    )


def test_env_example_has_no_unknown_vars():
    """反向：.env.example 不得殘留 config.py 沒有的變數（動態鍵家族範例除外）。"""
    unknown = sorted(v for v in _env_vars() - _config_vars() if not _DYNAMIC_FAMILY_RE.fullmatch(v))
    assert (
        not unknown
    ), f".env.example 有 {len(unknown)} 個 config.py 不認得的變數（打錯字或改名殘留？）：{unknown}"


def test_exempt_list_not_stale():
    """豁免項必須仍存在於 config.py 且不在 .env.example（否則豁免已無意義，該移除）。"""
    cfg, env = _config_vars(), _env_vars()
    stale = sorted(v for v in EXEMPT if v not in cfg or v in env)
    assert not stale, f"豁免清單過時（config 已無、或 .env.example 已列出）：{stale}"
