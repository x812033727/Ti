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
    kind: str = "text"  # text | password | select | combo（有建議選項但接受任意輸入）
    secret: bool = False
    options: tuple[str, ...] = ()
    placeholder: str = ""
    group: str = ""
    default: str = ""  # env 未設定時 UI 應顯示的「有效預設」（避免 select 誤顯第一個選項）
    recommended: str = ""  # 推薦值（UI 在選項加「（推薦）」尾綴、「套用推薦」一鍵填入）


# Claude 官方模型 ID（2026-06 現行清單）。select 嚴格白名單，由 update() 驗證。
CLAUDE_MODELS: tuple[str, ...] = (
    "claude-fable-5",
    "claude-opus-4-8",
    "claude-opus-4-7",
    "claude-opus-4-6",
    "claude-opus-4-5",
    "claude-sonnet-4-6",
    "claude-sonnet-4-5",
    "claude-haiku-4-5",
)

# OpenAI 模型僅為「建議值」：OPENAI_BASE_URL 可指向本地模型（Ollama / LM Studio），
# 模型名稱可能是任意字串，故用 combo（可選可打）而非嚴格 select。
OPENAI_MODELS: tuple[str, ...] = ("gpt-4o", "gpt-4o-mini")

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
        kind="select",
        options=CLAUDE_MODELS,
        default="claude-opus-4-8",  # 與 config.MODEL_LEAD 預設一致
        group="Claude",
    ),
    Field(
        "TI_MODEL_FAST",
        "Claude 快速模型（工程師／QA）",
        kind="select",
        options=CLAUDE_MODELS,
        default="claude-sonnet-4-6",  # 與 config.MODEL_FAST 預設一致
        group="Claude",
    ),
    # 每個角色可分開覆寫模型（auto＝沿用上面主力/快速的二分法；僅 Claude provider 適用）。
    # 推薦值＝品質優先（全員 claude-fable-5），前端「✨ 套用推薦模型」一鍵填入。
    *(
        Field(
            f"TI_MODEL_{key.upper()}",
            f"{zh}模型（auto＝依主力/快速規則）",
            kind="select",
            options=("auto", *CLAUDE_MODELS),
            default="auto",
            recommended="claude-fable-5",
            group="Claude",
        )
        for key, zh in (
            ("pm", "專案經理"),
            ("engineer", "工程師"),
            ("qa", "驗證工程師"),
            ("senior", "高級工程師"),
            ("researcher", "研究員"),
            ("architect", "架構師"),
            ("security", "資安審查"),
            ("devops", "整合維運"),
        )
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
    Field(
        "TI_OPENAI_MODEL_LEAD",
        "OpenAI 主力模型",
        kind="combo",
        options=OPENAI_MODELS,
        placeholder="gpt-4o",
        group="OpenAI",
    ),
    Field(
        "TI_OPENAI_MODEL_FAST",
        "OpenAI 快速模型",
        kind="combo",
        options=OPENAI_MODELS,
        placeholder="gpt-4o-mini",
        group="OpenAI",
    ),
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
        "需求澄清（拆解前 PM 先反問關鍵問題，逾時按假設續行，預設開）",
        kind="select",
        options=("0", "1"),
        default="1",
        group="進階",
    ),
    Field(
        "TI_CLARIFY_TIMEOUT",
        "澄清等待回覆秒數（逾時按 PM 預設假設續行）",
        placeholder="180",
        group="進階",
    ),
    Field(
        "TI_HUDDLE",
        "卡關討論 huddle（跑滿輪數仍未過時召集團隊找替代方案）",
        kind="select",
        options=("0", "1"),
        default="1",
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
        default="1",
        group="進階",
    ),
    Field(
        "TI_LESSONS",
        "跨場次教訓庫（長期記憶，開場注入 PM 拆解）",
        kind="select",
        options=("0", "1"),
        default="1",
        group="進階",
    ),
    Field(
        "TI_REFLEXION",
        "任務級反思記憶（失敗輪蒸餾反思，後續輪／huddle 重試帶回）",
        kind="select",
        options=("0", "1"),
        default="1",
        group="進階",
    ),
    Field(
        "TI_OBJECTIVE_GATE",
        "客觀驗收閘門（0 關／1 自測實敗才否決／strict 連未宣告指令也退回）",
        kind="select",
        options=("0", "1", "strict"),
        default="1",
        group="進階",
    ),
    Field(
        "TI_SELF_REFINE_ITERS",
        "單輪內自我精修次數（自測未過就地再修，0 關）",
        kind="select",
        options=("0", "1", "2", "3"),
        default="1",
        group="進階",
    ),
    Field(
        "TI_TURN_IDLE_TIMEOUT",
        "發言閒置逾時秒數（串流多久無進展即中止該輪發言，0 關）",
        placeholder="240",
        group="進階",
    ),
    Field(
        "TI_TURN_TIMEOUT",
        "發言總時長上限秒數（單次發言的硬上限兜底，0 關）",
        placeholder="1800",
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
        "TI_KNOWLEDGE",
        "知識沉澱（調研結論寫入 docs/RESEARCH.md，跨場次累積，預設開）",
        kind="select",
        options=("0", "1"),
        default="1",
        group="進階",
    ),
    Field(
        "TI_DISCOVER_ROLES",
        "找問題視角（csv：senior 工程／pm 產品／researcher 調研）",
        placeholder="senior,pm,researcher",
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
    Field(
        "TI_DISCUSS_MODE",
        "討論模式（legacy 循序兩人辯論／round_robin 多角色依序／parallel 同輪並行；"
        "預設並行，含架構討論與卡關 huddle）",
        kind="select",
        options=("legacy", "round_robin", "parallel"),
        default="parallel",
        group="進階",
    ),
    Field(
        "TI_DISCUSS_MAX_ROUNDS",
        "多角色討論最大輪數（空＝同辯論輪數 TI_DEBATE_ROUNDS）",
        placeholder="2",
        group="進階",
    ),
    Field(
        "TI_AGENDA_ROUNDS",
        "議程多子題時每子題討論輪數（空/非法＝1；單子題沿用討論最大輪數）",
        placeholder="1",
        group="進階",
    ),
    Field(
        "TI_ROLES_DIR",
        "自訂角色檔目錄（roles/*.md，內建為預設、同 key 覆蓋；空＝專案根 roles/）",
        placeholder="roles",
        group="進階",
    ),
    Field(
        "TI_RESEARCH_TOOLS",
        "實作中即時研究（工程師／高工附加 WebSearch/WebFetch，預設關）",
        kind="select",
        options=("0", "1"),
        default="0",
        group="進階",
    ),
    Field(
        "TI_RESEARCH_ALLOWED_DOMAINS",
        "研究網域白名單（csv，空＝不限網域但永擋私網）",
        placeholder="docs.python.org,developer.mozilla.org",
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
                "recommended": f.recommended,
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
