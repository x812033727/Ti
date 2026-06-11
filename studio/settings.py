"""執行期設定：讀取 / 更新「可由 UI 調整」的設定，並持久化到專案根目錄的 .env。

安全性：
- 秘密欄位（API key / token）讀取時**不回傳明文**，只回報是否已設定。
- 更新只接受白名單內的環境變數（FIELDS），未知鍵一律忽略。
- 秘密欄位留空＝不變更（避免清空既有金鑰）。
更新後呼叫 config.reload()，讓變更於下次討論即時生效，無需重啟。
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from . import config
from .secretfile import write_secret_file


@dataclass(frozen=True)
class Field:
    env: str
    label: str
    kind: str = "text"  # text | password | select
    secret: bool = False
    options: tuple[str, ...] = ()
    placeholder: str = ""
    group: str = ""
    default: str = ""  # env 未設定時 UI 應顯示的「有效預設」（避免 select 誤顯第一個選項）


FIELDS: tuple[Field, ...] = (
    Field(
        "TI_PROVIDER",
        "後端 Provider",
        kind="select",
        options=("claude", "openai"),
        default="claude",
        group="一般",
    ),
    Field(
        "ANTHROPIC_API_KEY",
        "Claude API Key",
        kind="password",
        secret=True,
        placeholder="sk-ant-...",
        group="Claude",
    ),
    Field(
        "TI_MODEL_LEAD",
        "Claude 主力模型（PM／高級工程師）",
        placeholder="claude-opus-4-8",
        group="Claude",
    ),
    Field(
        "TI_MODEL_FAST",
        "Claude 快速模型（工程師／QA）",
        placeholder="claude-sonnet-4-6",
        group="Claude",
    ),
    Field(
        "OPENAI_API_KEY",
        "OpenAI API Key",
        kind="password",
        secret=True,
        placeholder="sk-...",
        group="OpenAI",
    ),
    Field(
        "OPENAI_BASE_URL",
        "OpenAI Base URL（本地模型可填）",
        placeholder="http://localhost:11434/v1",
        group="OpenAI",
    ),
    Field("TI_OPENAI_MODEL_LEAD", "OpenAI 主力模型", placeholder="gpt-4o", group="OpenAI"),
    Field("TI_OPENAI_MODEL_FAST", "OpenAI 快速模型", placeholder="gpt-4o-mini", group="OpenAI"),
    Field(
        "GITHUB_TOKEN",
        "GitHub Token（clone 私有 repo／發佈成果）",
        kind="password",
        secret=True,
        placeholder="ghp_...",
        group="GitHub",
    ),
    Field(
        "TI_PUBLISH_REPO", "發佈目標 repo（owner/repo）", placeholder="owner/repo", group="GitHub"
    ),
    Field(
        "TI_PUBLISH_MERGE",
        "發佈後自動合併 PR（1 開／0 關）",
        kind="select",
        options=("0", "1"),
        default="0",
        group="GitHub",
    ),
    Field(
        "TI_PARALLEL_TASKS",
        "任務並行（獨立任務分波多支線同時做，1 開／0 關）",
        kind="select",
        options=("0", "1"),
        default="1",
        group="並行",
    ),
    Field(
        "TI_PARALLEL_LANES",
        "並行支線數上限（每波次同時進行的任務數）",
        kind="select",
        options=("1", "2", "3", "4", "5", "6"),
        default="3",
        group="並行",
    ),
    # --- 進階流程開關（對應 .env 的 power-user 旋鈕；消費端讀即時全域值，存檔後下次討論生效）---
    Field(
        "TI_CLARIFY",
        "需求澄清（拆解前 PM 先反問關鍵問題，逾時按假設續行）",
        kind="select",
        options=("0", "1"),
        default="0",
        group="進階",
    ),
    Field(
        "TI_HUDDLE",
        "卡關討論 huddle（跑滿輪數仍未過時召集團隊找替代方案）",
        kind="select",
        options=("0", "1"),
        default="0",
        group="進階",
    ),
    Field(
        "TI_CRITIC",
        "異議檢查 critic（放行前由獨立 critic 挑剔「為何還不算完成」）",
        kind="select",
        options=("0", "1"),
        default="0",
        group="進階",
    ),
    Field(
        "TI_NOTES",
        "共用筆記 NOTES.md（跨任務累積踩過的坑／決策）",
        kind="select",
        options=("0", "1"),
        default="0",
        group="進階",
    ),
    Field(
        "TI_LESSONS",
        "跨場次教訓庫（長期記憶，開場注入 PM 拆解）",
        kind="select",
        options=("0", "1"),
        default="0",
        group="進階",
    ),
    Field(
        "TI_REFLEXION",
        "任務級反思記憶（失敗輪蒸餾反思，後續輪／huddle 重試帶回）",
        kind="select",
        options=("0", "1"),
        default="0",
        group="進階",
    ),
    Field(
        "TI_OBJECTIVE_GATE",
        "客觀驗收閘門（0 關／1 自測實敗才否決／strict 連未宣告指令也退回）",
        kind="select",
        options=("0", "1", "strict"),
        default="0",
        group="進階",
    ),
    Field(
        "TI_SELF_REFINE_ITERS",
        "單輪內自我精修次數（自測未過就地再修，0 關）",
        kind="select",
        options=("0", "1", "2", "3"),
        default="0",
        group="進階",
    ),
    Field(
        "TI_RLIMITS",
        "子進程資源上限（記憶體／CPU／檔案大小防線，預設開）",
        kind="select",
        options=("0", "1"),
        default="1",
        group="進階",
    ),
    Field(
        "TI_BLUEPRINT",
        "產品藍圖（持續改良開跑時 PM 展開願景成藍圖，功能餵 backlog）",
        kind="select",
        options=("0", "1"),
        default="0",
        group="進階",
    ),
    Field(
        "TI_ADR",
        "架構決策記錄 ADR（辯論結論落盤 DECISIONS.md，跨場注入防翻案）",
        kind="select",
        options=("0", "1"),
        default="0",
        group="進階",
    ),
)

ALLOWED = {f.env for f in FIELDS}
_BY_ENV = {f.env: f for f in FIELDS}


def env_path() -> str:
    return config.env_path()


def read() -> dict:
    """回傳目前設定狀態給 UI；秘密欄位不含明文，只回報是否已設定。"""
    fields = []
    for f in FIELDS:
        raw = os.getenv(f.env, "")
        # 顯示值：env 未設定時退回該欄位的「有效預設」，避免 select 誤顯第一個選項
        # （如 TI_RLIMITS 預設開＝"1"）。set 仍依「env 是否實際設定」判斷（秘密欄位佔位提示用）。
        cur = raw if raw != "" else f.default
        fields.append(
            {
                "env": f.env,
                "label": f.label,
                "kind": f.kind,
                "secret": f.secret,
                "options": list(f.options),
                "placeholder": f.placeholder,
                "group": f.group,
                "value": "" if f.secret else cur,
                "set": bool(raw),
            }
        )
    return {"fields": fields}


def update(payload: dict) -> dict:
    """套用設定變更：寫入 .env、更新行程環境變數，並 reload config。回傳新狀態。"""
    path = env_path()
    for key, raw in (payload or {}).items():
        f = _BY_ENV.get(key)
        if f is None:  # 只接受白名單內的鍵
            continue
        val = ("" if raw is None else str(raw)).strip()
        if f.secret and val == "":
            continue  # 秘密留空＝不變更
        if f.kind == "select" and f.options and val not in f.options:
            continue  # 不接受非法選項
        write_secret_file(path, key, val)
        os.environ[key] = val
    config.reload()
    return read()
