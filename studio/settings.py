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

from dotenv import set_key

from . import config


@dataclass(frozen=True)
class Field:
    env: str
    label: str
    kind: str = "text"  # text | password | select
    secret: bool = False
    options: tuple[str, ...] = ()
    placeholder: str = ""
    group: str = ""


FIELDS: tuple[Field, ...] = (
    Field(
        "TI_PROVIDER", "後端 Provider", kind="select", options=("claude", "openai"), group="一般"
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
        group="GitHub",
    ),
)

ALLOWED = {f.env for f in FIELDS}
_BY_ENV = {f.env: f for f in FIELDS}


def env_path() -> str:
    return str(config.PROJECT_ROOT / ".env")


def read() -> dict:
    """回傳目前設定狀態給 UI；秘密欄位不含明文，只回報是否已設定。"""
    fields = []
    for f in FIELDS:
        cur = os.getenv(f.env, "")
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
                "set": bool(cur),
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
        set_key(path, key, val)
        os.environ[key] = val
    config.reload()
    return read()
