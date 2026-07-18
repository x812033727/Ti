"""集中設定。模型 ID、討論輪數、伺服器與 workspace 路徑都放這裡，方便日後調整。"""

from __future__ import annotations

import ipaddress
import logging
import os
import secrets
import shutil
from pathlib import Path

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

# 專案根（單一來源；env_path() 與 load_dotenv 共用）。
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 與 env_path()（settings/auth 的寫入端）同一路徑：固定載入專案根的 .env。
# 不帶路徑的 load_dotenv() 會從 cwd 向上搜尋，在 worktree/子目錄跑測試時會載到
# 上層部署環境的 .env（如門禁密碼），造成「寫入與載入路徑不一致」與測試環境污染。
load_dotenv(PROJECT_ROOT / ".env")


def _env_float(name: str, default: float) -> float:
    """讀數值環境變數，空字串／空白／無法解析時退回 default。

    .env 常出現 `TI_TURN_IDLE_TIMEOUT=''` 這種「設了但留空」的寫法——os.getenv 會回
    空字串而非預設，直接 float('') 會在 import 期炸掉整個服務啟動。此處統一容錯。
    """
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("環境變數 %s=%r 非數值，改用預設 %s", name, raw, default)
        return default


def _env_int(name: str, default: int) -> int:
    """讀整數環境變數，空字串／空白／無法解析時退回 default（同 _env_float 的容錯）。"""
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("環境變數 %s=%r 非整數，改用預設 %s", name, raw, default)
        return default


# --- Provider / 模型 ----------------------------------------------------
# 後端 LLM provider：claude（預設，走 Agent SDK 自帶工具）、openai（含 OpenAI 相容/本地模型）、
# minimax（MiniMax 訂閱／API key，走 OpenAI 相容介面）、gemini（Gemini OpenAI 相容端點）、
# codex（Codex CLI 非互動模式），或 antigravity（Google Antigravity CLI 非互動模式）。
PROVIDER = os.getenv("TI_PROVIDER", "claude").lower()

# Claude 模型 ID。PM / 高級工程師需要較強的推理；工程師 / 驗證工程師偏重速度。
MODEL_LEAD = os.getenv("TI_MODEL_LEAD", "claude-opus-4-8")
MODEL_FAST = os.getenv("TI_MODEL_FAST", "claude-sonnet-4-6")

# 每個角色可分開覆寫模型（TI_MODEL_<角色KEY大寫>，如 TI_MODEL_ENGINEER）。
# 值為空或 "auto" ＝ 不覆寫，沿用 LEAD_ROLES → MODEL_LEAD/FAST 的二分法（向下相容）。
# 角色 key 清單定義在此而非 roles.py，避免 config ↔ roles 循環 import。
ROLE_KEYS = ("pm", "engineer", "qa", "senior", "researcher", "architect", "security", "devops")


def _role_models() -> dict[str, str]:
    out: dict[str, str] = {}
    for key in ROLE_KEYS:
        val = os.getenv(f"TI_MODEL_{key.upper()}", "").strip()
        out[key] = "" if val in ("", "auto") else val
    return out


ROLE_MODELS = _role_models()

# 合法的 provider 名單；per-role 覆寫只接受其一，其餘（含 auto／空）＝不覆寫。
PROVIDERS = ("claude", "minimax", "codex", "antigravity")

# auto 派工模式（dispatch_auto()）下 PM 全權派工的 provider 子集與受限門檻：
# PM 只能在這兩家之間分配、模型 ID 直通不查白名單；僅當指定家不可用或用量達門檻才兜底改派。
AUTO_DISPATCH_PROVIDERS = ("claude", "codex")
AUTO_DISPATCH_THRESHOLD = 95.0


def _role_providers() -> dict[str, str]:
    """每個角色可單獨指定 provider（TI_PROVIDER_<角色KEY大寫>），達成 Claude／MiniMax 混用。

    值為空、"auto" 或非法（不在 PROVIDERS 內）＝不覆寫，沿用全域 PROVIDER。
    """
    out: dict[str, str] = {}
    for key in ROLE_KEYS:
        val = os.getenv(f"TI_PROVIDER_{key.upper()}", "").strip().lower()
        out[key] = val if val in PROVIDERS else ""
    return out


ROLE_PROVIDERS = _role_providers()


def role_provider(key: str) -> str:
    """角色的 per-role provider 覆寫（無覆寫回 ""）。"""
    return ROLE_PROVIDERS.get(key, "")


# PM 釘選 provider／模型：PM 是分派、檢驗與表決的最終決策者，判斷品質必須穩定，
# 故預設釘在 claude + claude-fable-5，不隨全域 provider、per-role 覆寫或動態派工漂移。
# 設為空字串＝解除釘選（provider 回到 TI_PROVIDER_PM → TI_PROVIDER 的一般優先序、
# model 回到 TI_MODEL_PM → LEAD/FAST 二分法）。釘選 provider 非法（不在 PROVIDERS 內）視同解除。
PM_PIN_PROVIDER = os.getenv("TI_PM_PIN_PROVIDER", "claude").strip().lower()
PM_PIN_MODEL = os.getenv("TI_PM_PIN_MODEL", "claude-fable-5").strip()

# Claude 訂閱對 Fable 等模型另設「按模型 scoped」的週限（與 5h/7d 全域窗獨立，見
# claude_usage._scoped_models）。當在線帳號對「本場某 claude 專家要用的模型」的 scoped 週限
# 達門檻時，_model_for 自動改派到「非 scoped」的備援模型（預設 Opus，走全域 weekly 額度）：
# 否則所有 claude 專家（含釘 Fable 的 PM）會一直撞滿額度、任務退回 pending 空轉，直到週限重置。
# 這是額度閘門（provider_quota，只看全域 5h/7d）的盲點補丁——它刻意不把 scoped 週限算進
# provider max_used（「Fable 滿 ≠ claude 受限」），代價就是模型被釘死在滿額 scoped 時無人接手。
# 空字串＝關閉自動改派（回舊行為）；備援模型自身若也 scoped 達門檻則不改派（改無益，交回閘門）。
CLAUDE_SCOPED_FALLBACK_MODEL = os.getenv(
    "TI_CLAUDE_SCOPED_FALLBACK_MODEL", "claude-opus-4-8"
).strip()
CLAUDE_SCOPED_LIMIT_THRESHOLD = float(os.getenv("TI_CLAUDE_SCOPED_LIMIT_THRESHOLD", "95"))


# OpenAI（相容）設定。OPENAI_BASE_URL 可指向本地模型（Ollama / LM Studio 等）。
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "")
OPENAI_MODEL_LEAD = os.getenv("TI_OPENAI_MODEL_LEAD", "gpt-4o")
OPENAI_MODEL_FAST = os.getenv("TI_OPENAI_MODEL_FAST", "gpt-4o-mini")
OPENAI_MAX_STEPS = int(os.getenv("TI_OPENAI_MAX_STEPS", "12"))

# MiniMax（OpenAI 相容介面；訂閱或 API key 皆走此路）。base_url 預設官方端點、可改；
# 模型 ID 走 MiniMax 自家命名（如 MiniMax-M3）。憑證與 OpenAI 分開存放，互不污染。
MINIMAX_API_KEY = os.getenv("MINIMAX_API_KEY", "")
MINIMAX_BASE_URL = os.getenv("MINIMAX_BASE_URL", "https://api.minimax.io/v1")
MINIMAX_MODEL_LEAD = os.getenv("TI_MINIMAX_MODEL_LEAD", "MiniMax-M3")
MINIMAX_MODEL_FAST = os.getenv("TI_MINIMAX_MODEL_FAST", "MiniMax-M3")

# Gemini（Google AI Studio API key；走官方 OpenAI 相容端點，重用 OpenAIExpert 工具迴圈）。
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_BASE_URL = os.getenv(
    "GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai/"
)
GEMINI_MODEL_LEAD = os.getenv("TI_GEMINI_MODEL_LEAD", "gemini-2.5-pro")
GEMINI_MODEL_FAST = os.getenv("TI_GEMINI_MODEL_FAST", "gemini-2.5-flash")

# Codex CLI（`codex exec`）設定。模型留空＝沿用 Codex CLI 自身設定；CODEX_API_KEY 僅由
# codex exec 支援，亦可沿用已登入的 CODEX_HOME/auth.json。
CODEX_BIN = os.getenv("TI_CODEX_BIN", "codex")
CODEX_HOME = os.getenv("CODEX_HOME", "")
CODEX_API_KEY = os.getenv("CODEX_API_KEY", "")
CODEX_MODEL_LEAD = os.getenv("TI_CODEX_MODEL_LEAD", "")
CODEX_MODEL_FAST = os.getenv("TI_CODEX_MODEL_FAST", "")
CODEX_SANDBOX_MODES = ("auto", "read-only", "workspace-write", "danger-full-access")


def _codex_sandbox() -> str:
    """Codex CLI sandbox 模式；auto 代表依角色工具白名單自動選 read-only/workspace-write。"""
    raw = (os.getenv("TI_CODEX_SANDBOX", "auto") or "auto").strip().lower()
    if raw not in CODEX_SANDBOX_MODES:
        logger.warning("TI_CODEX_SANDBOX=%r 不在白名單 %s，改用 auto", raw, CODEX_SANDBOX_MODES)
        return "auto"
    return raw


CODEX_SANDBOX = _codex_sandbox()
# 真正停用 Codex CLI sandbox/approval 的高風險逃生口；預設關閉。
CODEX_BYPASS_SANDBOX = os.getenv("TI_CODEX_BYPASS_SANDBOX", "0") not in (
    "0",
    "false",
    "False",
    "",
)

# Antigravity CLI（`agy -p`）設定。模型留空＝沿用 Antigravity CLI 自身設定；憑證與額度
# 由 `agy` 的 Google OAuth / Google Cloud project 登入管理，不使用 API key。
ANTIGRAVITY_BIN = os.getenv("TI_ANTIGRAVITY_BIN", "agy")
ANTIGRAVITY_MODEL_LEAD = os.getenv("TI_ANTIGRAVITY_MODEL_LEAD", "")
ANTIGRAVITY_MODEL_FAST = os.getenv("TI_ANTIGRAVITY_MODEL_FAST", "")
ANTIGRAVITY_SANDBOX = os.getenv("TI_ANTIGRAVITY_SANDBOX", "1") not in (
    "0",
    "false",
    "False",
    "",
)
ANTIGRAVITY_SKIP_PERMISSIONS = os.getenv("TI_ANTIGRAVITY_SKIP_PERMISSIONS", "1") not in (
    "0",
    "false",
    "False",
    "",
)

# --- 流程 ---------------------------------------------------------------
# 每個任務「實作→驗證→審查」的最大改進輪數，避免無止盡迴圈。
TASK_MAX_ROUNDS = int(os.getenv("TI_MAX_ROUNDS", "3"))
MAX_ROUNDS = TASK_MAX_ROUNDS  # 舊名相容

# 架構辯論的來回回合數（工程師 ⇄ 高級工程師）。
DEBATE_ROUNDS = int(os.getenv("TI_DEBATE_ROUNDS", "2"))


def _discuss_max_rounds() -> int:
    """DiscussionEngine 的最大輪數上限：TI_DISCUSS_MAX_ROUNDS，未設/留空/非法（含 <1）
    一律退回 DEBATE_ROUNDS（與舊辯論輪數對齊，向後相容）。"""
    raw = (os.getenv("TI_DISCUSS_MAX_ROUNDS") or "").strip()
    if not raw:
        return DEBATE_ROUNDS
    try:
        val = int(raw)
    except ValueError:
        logger.warning(
            "環境變數 TI_DISCUSS_MAX_ROUNDS=%r 非整數，改用 DEBATE_ROUNDS=%s", raw, DEBATE_ROUNDS
        )
        return DEBATE_ROUNDS
    if val < 1:
        logger.warning(
            "環境變數 TI_DISCUSS_MAX_ROUNDS=%s 須 ≥1，改用 DEBATE_ROUNDS=%s", val, DEBATE_ROUNDS
        )
        return DEBATE_ROUNDS
    return val


# 多角色討論（DiscussionEngine）的最大輪數上限；預設取 DEBATE_ROUNDS。
DISCUSS_MAX_ROUNDS = _discuss_max_rounds()

# 多角色討論模式白名單：legacy＝舊「工程師⇄高級工程師」兩人往返（opt-out 逃生口）；
# round_robin＝DiscussionEngine 依序發言；parallel＝同輪並行、輪間同步（**新預設**）。
DISCUSS_MODES = ("legacy", "round_robin", "parallel")
# 未設／留空時採用的預設模式。改為 parallel：架構討論與卡關 huddle 預設皆同輪並行（角色同時動工）。
DISCUSS_MODE_DEFAULT = "parallel"


def _discuss_mode() -> str:
    """TI_DISCUSS_MODE 解析：

    - 未設／留空／純空白 → 採新預設 DISCUSS_MODE_DEFAULT（parallel）；留空＝「未設定」，
      須等同未設（沿用本檔 .env 留空慣例），不可變成第三種行為。
    - 非空但非法（拼錯／大小寫／round-robin）→ 安全退回 legacy 並記 warning
      （守住「絕不誤開新路徑」：打錯字是未知意圖，退保守路徑而非新預設）。
    """
    raw = (os.getenv("TI_DISCUSS_MODE") or "").strip()
    if not raw:
        return DISCUSS_MODE_DEFAULT
    if raw not in DISCUSS_MODES:
        logger.warning("環境變數 TI_DISCUSS_MODE=%r 不在白名單 %s，改用 legacy", raw, DISCUSS_MODES)
        return "legacy"
    return raw


DISCUSS_MODE = _discuss_mode()


def _agenda_rounds() -> int:
    """逐子題討論的每子題輪數：TI_AGENDA_ROUNDS，未設/留空/非法（含 <1）一律 1。

    成本上界＝MAX_AGENDA_ITEMS(5) × AGENDA_ROUNDS(1)；prompt 的「2–5 個子題」只是
    建議不是防線，解析端另有硬截斷（flow.MAX_AGENDA_ITEMS）。"""
    raw = (os.getenv("TI_AGENDA_ROUNDS") or "").strip()
    if not raw:
        return 1
    try:
        val = int(raw)
    except ValueError:
        logger.warning("環境變數 TI_AGENDA_ROUNDS=%r 非整數，改用 1", raw)
        return 1
    if val < 1:
        logger.warning("環境變數 TI_AGENDA_ROUNDS=%s 須 ≥1，改用 1", val)
        return 1
    return val


# 多子題議程討論時，每個子題的討論輪數（單子題沿用 DISCUSS_MAX_ROUNDS）。
AGENDA_ROUNDS = _agenda_rounds()

# --- 內部討論機制（卡關 huddle）--------------------------------------------
# 開啟後：任務跑滿 TASK_MAX_ROUNDS 仍未通過時，召集團隊 huddle 找替代方案並給 1 輪重試，
# 仍失敗則明確標記為「已知限制」而非靜默帶過。只在「跑滿輪數仍失敗」的低頻路徑加成本，
# 換得失敗被明示——預設開啟（要省可關）。
HUDDLE_ENABLED = os.getenv("TI_HUDDLE", "1") not in ("0", "false", "False", "")

# 中途求助：工程師實作中輸出 `求助: <一句問題>` 時，就地讓 PM 給一次指示後續作（輪內輕量
# 通道，與跑滿輪數才觸發的 huddle 互補）。marker 為 opt-in 觸發——工程師不求助即零行為差；
# TASK_HELP_MAX 為「每任務」上限（非每輪），防多輪 × 多次求助燒 token。
TASK_HELP_ENABLED = os.getenv("TI_TASK_HELP", "1") not in ("0", "false", "False", "")
TASK_HELP_MAX = _env_int("TI_TASK_HELP_MAX", 1)

# 異議檢查（critic）：放行前由獨立 critic 專挑「為何還不算完成」，提出實質反對才退回。
# 採「換人」原則保獨立性（任務審查用 pm 視角、最終驗收用 senior 視角）。
# 唯一在「成功路徑」上加成本的學習開關（每個通過任務都多一次獨立呼叫）且有誤退回風險，
# 維持預設關閉（opt-in）。
CRITIC_ENABLED = os.getenv("TI_CRITIC", "0") not in ("0", "false", "False", "")

# critic 收斂預算：客觀閘門（qa／senior／security／交付前自測）全綠時，critic 至多退回 N 次；
# 連續退回達上限仍提不出可重現紅點 → 客觀證據優先，以「已知限制」放行並把殘留疑慮記成後續任務
# （不靜默丟），避免 critic 對 objectively-green 的票無限退回、燒滿輪數後整場判失敗。
# 0＝不設限（舊行為：critic 可在輪數內無限退回）。只作用於任務審查 gate，不影響最終驗收 gate。
CRITIC_MAX_REJECTS = int(os.getenv("TI_CRITIC_MAX_REJECTS", "2"))

# 動態流程（workflow）的 dynamic step：PM 運行時逐 hop 決定下一步找誰的最大 hop 數上限
# （stage 未指定 budget 時取此值）。空字串容錯（_env_int），對齊收斂預算思維防無限退回。
DYNAMIC_STEP_BUDGET = _env_int("TI_DYNAMIC_STEP_BUDGET", 3)

# 動態招募：單場 PM 最多招募幾位新成員（庫已有角色＋液生 persona 共用此上限），防 roster 爆量。
RECRUIT_MAX = _env_int("TI_RECRUIT_MAX", 3)

# 3-AI 表決：PM 於 dynamic step 無法決定時可發起 `表決: <議題> | <選項A> | <選項B>`，系統找
# 兩位「不同 provider」的一次性投票員與 PM 多數決（副作用集中在 orchestrator._hold_vote）。
# VOTE_MAX＝單場 session 表決次數上限（防 PM 把所有決策外包給表決、燒額度），超過即忽略請求。
VOTE_ENABLED = os.getenv("TI_VOTE_ENABLED", "1") not in ("0", "false", "False", "")
VOTE_MAX = _env_int("TI_VOTE_MAX", 2)

# 互動 session（WS，非 improve）未指定 workflow 時走的預設流程名。預設「動態優先」（dynamic-first）；
# 設空字串＝退回內建安全骨架。autopilot／improver 不讀此值（直接 workflow=None）。
DEFAULT_WORKFLOW = os.getenv("TI_DEFAULT_WORKFLOW", "動態優先").strip()

# 共用知識庫（workspace 內 NOTES.md）：跨任務累積踩過的坑/決策/後續，實作時讀回、結束時寫入。
# 不進交付物與檔案清單（見 workspace._IGNORE）。純檔案 IO、無額外 LLM 呼叫——預設開啟；
# 注入時只取尾段 NOTES_MAX_CHARS 字（從段落邊界起），防專案模式長跑 context 無限膨脹。
NOTES_ENABLED = os.getenv("TI_NOTES", "1") not in ("0", "false", "False", "")
NOTES_MAX_CHARS = int(os.getenv("TI_NOTES_MAX_CHARS", "6000"))

# 跨場次教訓庫（lessons.json）：工作室的長期記憶。每場檢討蒸餾出可重用的「教訓」持久化，
# 下次新討論開場注入 PM 拆解，讓工作室跨場次自我加強（避免重蹈、善用既有結論）。
# 近零成本（搭檢討 prompt 順帶解析，無額外 LLM 呼叫）——預設開啟；LESSONS_MAX 為注入上限。
LESSONS_ENABLED = os.getenv("TI_LESSONS", "1") not in ("0", "false", "False", "")
LESSONS_MAX = int(os.getenv("TI_LESSONS_MAX", "12"))
# 教訓庫蒸餾：庫超過門檻時於檢討後用一次 LLM 把相近教訓合併、淘汰過時項（取代純 FIFO 截斷的
# 粗暴遺忘）。低頻（門檻＋最小間隔雙閘）；LLM 失敗/離線/壞輸出一律靜默跳過、保留原庫，行為退
# 回現行 FIFO——絕不讓壞輸出清空長期記憶。env-only（與 LESSONS_MAX 同級的 power-user 旋鈕）。
LESSONS_DISTILL = os.getenv("TI_LESSONS_DISTILL", "1") not in ("0", "false", "False", "")
LESSONS_DISTILL_THRESHOLD = int(os.getenv("TI_LESSONS_DISTILL_THRESHOLD", "200"))
LESSONS_DISTILL_INTERVAL = int(os.getenv("TI_LESSONS_DISTILL_INTERVAL", "86400"))  # 最小間隔（秒）

# 考核庫（appraisals.json）：每場收尾檢討 PM 對各參與 AI 打 1–5 分（`考核:` 行），與客觀
# 指標（QA 輪數／裁決、高工核可、耗時）合併持久化；拆解與 per-task 派工時聚合成
# {provider: 平均分} 餵 flow.choose_dispatch（同用量偏好歷史表現好者）與 PM 拆解摘要。
# 近零成本（搭檢討 prompt 順帶解析，無額外 LLM 呼叫）——預設開啟；MAX_STORE 為檔案
# 保留上限（由新到舊裁剪，防長跑只增不減）。
APPRAISAL_ENABLED = os.getenv("TI_APPRAISAL", "1") not in ("0", "false", "False", "")
APPRAISAL_MAX_STORE = int(os.getenv("TI_APPRAISAL_MAX_STORE", "2000"))

# 需求澄清階段：拆解前 PM 先就模糊需求向使用者反問關鍵問題（附預設假設），等回覆逾時則按
# 假設續行——流程絕不因等人而卡死。僅互動 session 生效（須有插話佇列）；autopilot／持續改良
# 迴圈等自主流程一律跳過。預設開啟：這是「說一句產品就能開工」的核心，無插話佇列時
# 自動跳過、天然向後相容。結論固化 workspace 的 PRD.md，抽出的「願景:」回填專案 meta。
CLARIFY_ENABLED = os.getenv("TI_CLARIFY", "1") not in ("0", "false", "False", "")
CLARIFY_TIMEOUT = _env_float("TI_CLARIFY_TIMEOUT", 180.0)  # 等使用者回覆的秒數
CLARIFY_MAX_QUESTIONS = int(os.getenv("TI_CLARIFY_MAX_QUESTIONS", "4"))

# 知識沉澱（workspace 的 docs/RESEARCH.md；PRD.md 由澄清階段寫根、設計決策由 ADR 寫根）：
# 調研結論持久化成交付物，下場開場注入尾段——專案模式 workspace 固定，知識自然跨場次累積。
# 檔案不存在時注入空字串、行為與關閉時逐字相同，故可安全預設開啟。
KNOWLEDGE_ENABLED = os.getenv("TI_KNOWLEDGE", "1") not in ("0", "false", "False", "")
KNOWLEDGE_MAX_CHARS = int(os.getenv("TI_KNOWLEDGE_MAX_CHARS", "4000"))  # 注入尾段上限（字元）

# 產品藍圖：專案持續改良迴圈開跑時，PM 把一句願景展開成結構化藍圖（願景/用戶/功能 P0~P2/
# 里程碑），落盤 BLUEPRINT.md＋blueprint.json、功能清單餵入專案 backlog（P0 先做），
# 跨場次注入 requirement 前綴——讓「越做越進步」有方向感。每專案僅生成一次。
# 預設關閉（opt-in，會多一次 PM 呼叫）；SEED_MAX 為一次最多餵 backlog 的功能數。
BLUEPRINT_ENABLED = os.getenv("TI_BLUEPRINT", "0") not in ("0", "false", "False", "")
BLUEPRINT_SEED_MAX = int(os.getenv("TI_BLUEPRINT_SEED_MAX", "5"))

# 架構決策記錄（ADR）：架構辯論/架構師定案後蒸餾成決策條目，落盤 workspace 的
# DECISIONS.md（人讀、進交付物）＋adr.json（機讀索引）；後續 session 的 PM 拆解與
# 架構提案注入既有決策摘要，翻案須說明理由——避免跨場次反覆推翻。預設關閉（opt-in）。
ADR_ENABLED = os.getenv("TI_ADR", "0") not in ("0", "false", "False", "")
ADR_MAX = int(os.getenv("TI_ADR_MAX", "8"))  # 注入時取最新 N 筆決策

# 實作中即時研究（roadmap 階段二，opt-in 預設關）：開啟後工程師／高級工程師的工具清單
# 附加 WebSearch/WebFetch，動工中可上網查官方 API、套件用法與最佳實踐。Claude 路徑由
# SDK 原生支援；OpenAI function-calling 路徑由 tools.py 的 web_fetch 工具承接。
RESEARCH_TOOLS_ENABLED = os.getenv("TI_RESEARCH_TOOLS", "0") not in ("0", "false", "False", "")
# 研究網域白名單（csv，比對 hostname 尾綴）。空＝不限網域，但私網/loopback/link-local 等
# 位址永遠擋（SSRF 防護不受白名單影響）。涵蓋 OpenAI 工具層（web_fetch）與 Claude 路徑
# （WebFetch 經 can_use_tool 攔截）；Claude 的 WebSearch 流量不經本機、無法施加白名單（見 README）。
RESEARCH_ALLOWED_DOMAINS = [
    d.strip().lower() for d in os.getenv("TI_RESEARCH_ALLOWED_DOMAINS", "").split(",") if d.strip()
]
RESEARCH_FETCH_TIMEOUT = float(os.getenv("TI_RESEARCH_FETCH_TIMEOUT", "20"))  # 單次抓取逾時（秒）
RESEARCH_FETCH_MAX_CHARS = int(os.getenv("TI_RESEARCH_FETCH_MAX_CHARS", "8000"))  # 回應截斷上限

# --- 自我改進機制（移植自 ti-studio 自我進步交付，補主迴圈缺口）-----------------
# A 反思記憶：每輪失敗把 QA／高工意見蒸餾成精簡反思，存 per-session JSONL，後續輪次／huddle
#   重試時 prepend 回工程師 context（既有「上一輪原文回饋」照舊，本機制只補更早輪次的累積）。
# B 客觀閘門：交付前自測（smoke-run）實際執行失敗 → 該輪「強制退回」，不讓 QA／高工的文字裁決
#   推翻真實 exit code（守住反 reward-hacking）。
# C 子進程資源上限：runner 執行指令時套 RLIMIT，補 bwrap 沒有的記憶體／CPU／檔案大小防線。
# D Self-Refine：單輪內自測未過時，讓同一工程師就地依執行紀錄再修一次。
# 預設組合（讓「越做越進步」的迴圈真的在跑）：A／B／C／D 全開——A 只在失敗輪多一次廉價
# 呼叫且永不 raise；B 零 LLM 成本、只在「自測真的有跑且失敗」才否決（strict 仍 opt-in，
# 會誤殺純文件類任務）；D 失敗才觸發、一次就地修常省下整輪 QA＋審查三連呼叫。
# 與 TI_LESSONS／NOTES／HUDDLE／CRITIC 同列「進階流程」開關：env 仍是來源，且已納入設定面板
# （settings.FIELDS「進階」組）與 reload()。消費端皆讀即時全域值，故面板存檔後下次討論即生效。
REFLEXION_ENABLED = os.getenv("TI_REFLEXION", "1") not in ("0", "false", "False", "")
REFLEXION_MAX = int(os.getenv("TI_REFLEXION_MAX", "5"))  # 注入時取最近 N 筆反思
# 客觀閘門：0=關／1=開（工程師本輪宣告的自測指令實敗才否決；fallback 整體指令只回報不硬退）
# ／strict=fallback 失敗與「未宣告執行指令」皆視為未通過。
OBJECTIVE_GATE = os.getenv("TI_OBJECTIVE_GATE", "1")
SELF_REFINE_ITERS = int(os.getenv("TI_SELF_REFINE_ITERS", "1"))  # 單輪內就地精修次數（0=關）
# 子進程資源上限（穩健式預設開）。每項 0=略過該限。RLIMIT_AS 算虛擬位址空間，V8／BLAS 會預留
# 數 GB，故 4096MB 為真實工作負載的寬鬆下限（交付物 512MB 是玩具題尺度）；CPU 300s 遠高於
# DEMO_TIMEOUT(60s wall)，只攔失控孤兒；FSIZE 512MB 擋單檔塞爆磁碟而不卡 pip wheel。
RLIMITS_ENABLED = os.getenv("TI_RLIMITS", "1") not in ("0", "false", "False", "")
RLIMIT_MEM_MB = int(os.getenv("TI_RLIMIT_MEM_MB", "4096"))
RLIMIT_CPU_S = int(os.getenv("TI_RLIMIT_CPU_S", "300"))
RLIMIT_FSIZE_MB = int(os.getenv("TI_RLIMIT_FSIZE_MB", "512"))


def objective_gate_enabled() -> bool:
    """客觀閘門是否啟用（"1" 或 "strict"）。讀目前全域值，故 reload() 後即時生效。"""
    return OBJECTIVE_GATE in ("1", "strict")


def objective_gate_strict() -> bool:
    """嚴格模式：連「未宣告可執行指令」也視為未通過（無從客觀驗證＝不放行）。"""
    return OBJECTIVE_GATE == "strict"


# 停滯守門：改進迴圈連續 STALL_ROUNDS 輪只重述（文字高度相似且無檔案變動）就提早收斂，
# 避免燒 token。<=1 視為停用。預設值刻意大於離線示範每任務實際圈數，使既有流程不誤觸；
# 且 _stalled 在無 cwd 或關閉 git 時一律不偵測（保護 cwd=None 的單元測試）。
STALL_ROUNDS = int(os.getenv("TI_STALL_ROUNDS", "3"))

# 單一專家發言（含工具操作）的回合上限，避免 agent 卡住。
MAX_TURNS_PER_TURN = int(os.getenv("TI_MAX_TURNS", "40"))

# 發言層 watchdog：max_turns 只限「回合數」，限不住單一工具呼叫卡死（如前景跑常駐
# server），故另設時間軸保護。idle＝兩則串流訊息的間隔上限（有進展就重置，不誤殺正常
# 長發言）；hard＝整次發言的總時長兜底。各自 0=停用。逾時走 Expert._abort_turn 回收。
TURN_IDLE_TIMEOUT = _env_float("TI_TURN_IDLE_TIMEOUT", 240)
TURN_HARD_TIMEOUT = _env_float("TI_TURN_TIMEOUT", 1800)

# 對偵測到的 rate_limit_error／429 做有限次退避重試——優先讀 retry-after，否則指數退避
EXPERT_RATE_LIMIT_RETRIES = int(os.getenv("TI_RATELIMIT_RETRIES", "3"))
EXPERT_RATE_LIMIT_BACKOFF = _env_float("TI_RATELIMIT_BACKOFF", 2.0)  # 退避基數（秒）
EXPERT_RATE_LIMIT_BACKOFF_CAP = _env_float("TI_RATELIMIT_BACKOFF_CAP", 60.0)  # 單次退避上限
# 退避 jitter 分數 ∈[0,1]，傳給 llm_caller.backoff_delay 打散多 expert 同撞 429／529 的重試時點
# （避免 thundering herd）。預設 0.5（equal-jitter，落點 [nominal×0.5, nominal]）；設 0 關閉、
# 回純指數退避與舊行為等價。retry_after 分支只向上微抖、永不早於伺服器建議值。
EXPERT_RATE_LIMIT_BACKOFF_JITTER = _env_float("TI_RATELIMIT_BACKOFF_JITTER", 0.5)

# 啟用哪些「可選角色」（核心 4 角色永遠在）。逗號分隔；清空則只剩核心 4 角色。
# 多一個角色 = 每場討論多幾次 LLM 呼叫（更耗額度、更久），要省可逐一移除。
OPTIONAL_ROLES = {
    r.strip()
    for r in os.getenv("TI_OPTIONAL_ROLES", "researcher,architect,security,devops").split(",")
    if r.strip()
}

# 哪些角色用「主力（強但慢）」模型 MODEL_LEAD，其餘一律用 MODEL_FAST（快）。
# 為加速,預設只剩 pm（規劃/驗收需強推理）；要重品質可加 senior,architect,security。
LEAD_ROLES = {r.strip() for r in os.getenv("TI_LEAD_ROLES", "pm").split(",") if r.strip()}

# PM 拆解出的任務數上限（autopilot 單一 backlog 任務不該再炸成超多子任務 → 控時間）。
MAX_TASKS = int(os.getenv("TI_MAX_TASKS", "5"))

# --- 任務並行（多支線 lane）---------------------------------------------
# 開啟後：PM 標注依賴 → 獨立任務分「波次」，每波最多 PARALLEL_LANES 條支線並行，每條各有
# 獨立 git worktree 分支與專家團隊，完工依序合併回主分支。功能已成熟（波次/lane/合併衝突
# 化解/lane 例外降級/worktree 洩漏兜底/可觀測指標皆備且有測試），故預設開啟；要還原純循序
# 行為：TI_PARALLEL_TASKS=0（循序專屬語義的測試已各自明確釘在 PARALLEL_TASKS_ENABLED=False）。
PARALLEL_TASKS_ENABLED = os.getenv("TI_PARALLEL_TASKS", "1") not in ("0", "false", "False", "")
# 單一波次內同時並行的支線數上限（含 1 = 退化為循序）。
PARALLEL_LANES = int(os.getenv("TI_PARALLEL_LANES", "3"))
# 全域同時進行中的 LLM 發言數上限（節流：N 條 lane × 各自驗證/審查/資安 gather 可能爆量）。
# 下限會在使用時夾到 ≥ 單一 lane 內最大 gather 數（4），避免單 lane 內 gather 自我死鎖。
LLM_MAX_CONCURRENCY = int(os.getenv("TI_LLM_MAX_CONCURRENCY", "9"))

# --- 確定性執行（runner）-----------------------------------------------
# 自測 / Demo 的執行逾時（秒）與輸出字數上限。
DEMO_TIMEOUT = int(os.getenv("TI_DEMO_TIMEOUT", "60"))
DEMO_MAX_OUTPUT = int(os.getenv("TI_DEMO_MAX_OUTPUT", "8000"))

# 是否在 workspace 內建立獨立 git repo 並做階段性 commit。
ENABLE_GIT = os.getenv("TI_ENABLE_GIT", "1") not in ("0", "false", "False", "")

# --- 沙箱（隔離專家 / Demo 的指令執行，避免以 root 誤傷主機）---------------
# 開啟後：專家 bash 走 SDK 原生 sandbox（bubblewrap）、Demo 執行由 runner 用 bwrap
# 包住（新 PID namespace + 只有 workspace 可寫）。一鍵還原：TI_SANDBOX=0。
SANDBOX_ENABLED = os.getenv("TI_SANDBOX", "1") not in ("0", "false", "False", "")
# Demo 執行預設「無網路」；需要時設 TI_SANDBOX_NET=1（PID 隔離仍保護主機）。
SANDBOX_NET = os.getenv("TI_SANDBOX_NET", "0") not in ("0", "false", "False", "")
SANDBOX_BWRAP = os.getenv("TI_SANDBOX_BWRAP", "/usr/bin/bwrap")
_DEFAULT_SANDBOX_DOMAINS = (
    "pypi.org,files.pythonhosted.org,registry.npmjs.org,"
    "github.com,codeload.github.com,objects.githubusercontent.com"
)
SANDBOX_ALLOWED_DOMAINS = [
    d.strip()
    for d in os.getenv("TI_SANDBOX_ALLOWED_DOMAINS", _DEFAULT_SANDBOX_DOMAINS).split(",")
    if d.strip()
]


def _sandbox_available() -> bool:
    """bwrap 是否存在（runner 的 Demo 層用來 fail-closed）。"""
    return os.path.exists(SANDBOX_BWRAP)


def sandbox_missing_deps() -> list[str]:
    """沙箱啟用時所需的外部工具中缺少的項目。

    ⚠️ 重要：CLI 的原生沙箱在缺 socat/bwrap 時是「fail-open」——會【靜默停用沙箱、
    照常無限制執行】。所以伺服器啟動時要據此發出明顯警告，避免重佈後缺套件卻無人察覺。
    """
    if not SANDBOX_ENABLED:
        return []
    missing = []
    if not os.path.exists(SANDBOX_BWRAP):
        missing.append("bwrap")
    if shutil.which("socat") is None:
        missing.append("socat")
    return missing


def expert_sandbox_settings() -> dict | None:
    """給 ClaudeAgentOptions 的 SandboxSettings；停用時回 None（行為與改動前完全相同）。

    `enabled` 讓 CLI 用 bubblewrap 把專家的 bash 關進 PID namespace（殺不到主機進程）；
    `allowUnsandboxedCommands=False` 移除 dangerouslyDisableSandbox 逃生門；
    network.allowedDomains 僅放行套件來源（CLI 沙箱層才支援 per-domain）。
    """
    if not SANDBOX_ENABLED:
        return None
    return {
        "enabled": True,
        "autoAllowBashIfSandboxed": True,
        "allowUnsandboxedCommands": False,
        "excludedCommands": [],
        "network": {
            "allowedDomains": SANDBOX_ALLOWED_DOMAINS,
            "allowUnixSockets": [],
        },
    }


# --- 離線示範模式 -------------------------------------------------------
# 不需 API 金鑰，用腳本化的假專家驅動完整流程（真的寫檔/git/Demo），供試用與端到端驗證。
OFFLINE_MODE = os.getenv("TI_OFFLINE", "0") not in ("0", "false", "False", "")
OFFLINE_DELAY = float(os.getenv("TI_OFFLINE_DELAY", "0.4"))  # 每次發言之間的節奏（秒）

# --- 發佈到 GitHub（對外、預設關閉）------------------------------------
# 需同時設定 GITHUB_TOKEN 與 TI_PUBLISH_REPO（owner/repo）才會啟用。
# autopilot 自改路徑（clone/fetch/push CORE_REPO/AUTOPILOT_REPO）自 gh CLI helper 統一至
# git_cred 後，認證改綁 GITHUB_TOKEN：**須持有 AUTOPILOT_REPO 的 write 權限**；為空時
# autopilot 的 git 操作不帶任何認證（等同無 gh 登入的舊行為），私有 repo 會 clone/push 失敗。
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
# 緊急回退到舊 token-in-URL git 認證路徑；預設關閉，走 git_cred env 注入。
# publisher push 路徑需 git ≥ 2.31；legacy/舊 git 時 push 403 屬 fail-closed 預期。
# 下線判準：連續兩個 minor release 無 legacy=True 生產回報 → 刪閥
TI_GIT_CRED_LEGACY = os.getenv("TI_GIT_CRED_LEGACY", "0") not in (
    "0",
    "false",
    "False",
    "",
)
PUBLISH_REPO = os.getenv("TI_PUBLISH_REPO", "")  # 例：octocat/outputs
PUBLISH_BASE = os.getenv("TI_PUBLISH_BASE", "main")  # PR 目標分支


def _publish_owner_allowlist() -> frozenset[str]:
    """發佈／建庫 owner allowlist（csv → 小寫 frozenset）。

    autopilot/publisher 只允許對這些 owner 底下的 repo push／merge／deploy，
    以及在這些 owner 底下建立全新 repo；其他 owner 一律 fail-closed 擋下
    （絕不推送／污染任何其他既有 repo）。守門在 publisher.assert_repo_allowed。
    """
    raw = os.getenv("TI_PUBLISH_OWNER_ALLOWLIST", "x812033727")
    return frozenset(o.strip().lower() for o in raw.split(",") if o.strip())


PUBLISH_OWNER_ALLOWLIST = _publish_owner_allowlist()
# 專案完成後是否自動發佈（預設關閉，避免非預期的對外推送）。
PUBLISH_AUTO = os.getenv("TI_PUBLISH_AUTO", "0") not in ("0", "false", "False", "")
# push 並開 PR 後是否自動合併進 base 分支（預設關閉，向後相容；開啟才形成自我改進閉環）。
PUBLISH_MERGE = os.getenv("TI_PUBLISH_MERGE", "0") not in ("0", "false", "False", "")
# 自動合併前等待 CI 的最長秒數、輪詢間隔、以及對 stale／409 的重試次數。
PUBLISH_CI_TIMEOUT = int(os.getenv("TI_PUBLISH_CI_TIMEOUT", "600"))
PUBLISH_CI_INTERVAL = int(os.getenv("TI_PUBLISH_CI_INTERVAL", "10"))
PUBLISH_MERGE_RETRIES = int(os.getenv("TI_PUBLISH_MERGE_RETRIES", "3"))
# 合併時 PR 落後 base（mergeable_state=behind，分支保護要求與 base 同步→PUT merge 回 405）
# 的自動修復輪數：update-branch 把 base 併進來 → 等新 head 的 CI → 重試合併。
# 0＝停用（恢復舊行為：behind 直接判 CONFLICT 退回）；上限防止 base 高頻前進時無限追趕。
# 預設 2→4（第五輪 C1）：BEHIND 是「main 動太快」而非任務缺陷，2 輪在多 PR 排隊日
# （單日 12 支）實測容易耗盡誤傷；耗盡的處置同步改為退回 pending 不計 attempts。
MERGE_BEHIND_RETRIES = int(os.getenv("TI_MERGE_BEHIND_RETRIES", "4"))
# 發佈後 CI 失敗時，讓團隊修正重推、再驗合併的最多輪數；以及每輪等新 commit 的 check
# 註冊出現的寬限秒數（避免「尚未註冊」被誤判為無 CI 而提前合併）。
PUBLISH_CI_MAX_ROUNDS = int(os.getenv("TI_PUBLISH_CI_MAX_ROUNDS", "5"))
PUBLISH_CI_GRACE = int(os.getenv("TI_PUBLISH_CI_GRACE", "120"))

# --- 登入 / 門禁（單一共用密碼，預設關閉）------------------------------
# 設定 TI_ACCESS_PASSWORD 後即啟用門禁：使用者需在登入頁輸入正確密碼才能進入工作室。
# 留空（預設）則完全停用認證，本地開發與離線示範不受影響、向後相容。
ACCESS_PASSWORD = os.getenv("TI_ACCESS_PASSWORD", "")
# 簽發 session cookie 的密鑰。留空時於程序啟動產生一組記憶體內隨機值（重啟即失效所有登入）。
_env_auth_secret = os.getenv("TI_AUTH_SECRET", "")
AUTH_SECRET = _env_auth_secret or secrets.token_hex(32)
# 臨時隨機密鑰旗標；warning 延到服務啟動時才發（見 server._lifespan），避免在 config 被
# import/reload 的單元測試裡發出非預期 warning 污染其他測試的 caplog 斷言。
AUTH_SECRET_IS_EPHEMERAL = not _env_auth_secret
# 登入 cookie 名稱與有效秒數（預設 7 天）。
AUTH_COOKIE = "ti_session"
AUTH_TTL = int(os.getenv("TI_AUTH_TTL", "604800"))

# --- 工具讀檔上限：防超大生成檔被全量 read_text 載入記憶體（OOM）------------
# read_file 工具與 workspace.read_file 在讀取前先檢查檔案大小，超過即拒讀並回提示。
MAX_READ_FILE_BYTES = int(os.getenv("TI_MAX_READ_FILE_BYTES", str(1_000_000)))


def auth_enabled() -> bool:
    """是否啟用密碼門禁（設定了 TI_ACCESS_PASSWORD 才啟用）。"""
    return bool(ACCESS_PASSWORD)


# --- 可信代理 / 來源 IP（反代下判斷真實 client，預設關閉）-----------------
# TI_TRUST_PROXY 關閉（預設）時：完全忽略 X-Forwarded-For，只認 socket peer，向後相容。
# 開啟後：僅當請求來自 TI_TRUSTED_PROXIES 內的受信代理，才採信 XFF 並解析真實來源。
# ⚠️ 啟用時務必確保 app port 僅受信代理可連，否則 XFF 可被偽造繞過。
TRUST_PROXY = os.getenv("TI_TRUST_PROXY", "0") not in ("0", "false", "False", "")
# 受信代理 IP/CIDR 清單（逗號分隔）。預設僅 loopback（同機代理）。
TRUSTED_PROXIES = os.getenv("TI_TRUSTED_PROXIES", "127.0.0.0/8,::1")


def trust_proxy_enabled() -> bool:
    """是否採信反向代理附加的 X-Forwarded-For（設定 TI_TRUST_PROXY 才啟用）。"""
    return TRUST_PROXY


# 受信代理清單解析後的快取（lazy）；None 代表尚未解析。
_trusted_proxies_cache: list[ipaddress._BaseNetwork] | None = None


def trusted_proxies() -> list[ipaddress._BaseNetwork]:
    """將 TI_TRUSTED_PROXIES 解析為 ip_network 清單並快取。

    無效項目會 log 警告並略過該項（fail-safe），絕不因單項解析失敗退化為信任全部。
    正式執行視為程序啟動時固定，不做 runtime 熱更新；測試可用 reset_trusted_proxies() 清快取。
    """
    global _trusted_proxies_cache
    if _trusted_proxies_cache is None:
        networks: list[ipaddress._BaseNetwork] = []
        for item in TRUSTED_PROXIES.split(","):
            item = item.strip()
            if not item:
                continue
            try:
                networks.append(ipaddress.ip_network(item, strict=False))
            except ValueError:
                logger.warning("TI_TRUSTED_PROXIES 含無效項目，已略過: %r", item)
        _trusted_proxies_cache = networks
    return _trusted_proxies_cache


def reset_trusted_proxies() -> None:
    """清掉受信代理清單快取（供單元測試切換 TI_TRUSTED_PROXIES 後重算）。"""
    global TRUSTED_PROXIES, _trusted_proxies_cache
    TRUSTED_PROXIES = os.getenv("TI_TRUSTED_PROXIES", "127.0.0.0/8,::1")
    _trusted_proxies_cache = None


# --- uvicorn ProxyHeaders 信任來源（傳輸層，啟動時固定）---------------------
# 這是 uvicorn ProxyHeadersMiddleware 的設定：僅清單內來源送來的 X-Forwarded-* 會被
# 採信並改寫 ASGI scope（client IP / scheme）。與上面應用層的 TI_TRUST_PROXY /
# TI_TRUSTED_PROXIES（netutil 自行解析 XFF）互補、語意獨立、各自設定——一個在傳輸層
# 由 uvicorn 改寫 scope，一個在應用層由 netutil 解析真實來源。
# 預設僅信任本機；嚴禁 "*"（偵測到即拒啟動，見 forwarded_allow_ips()），否則攻擊者可自帶
# X-Forwarded-For 偽造 client IP，污染日誌、稽核、限流與 IP 白名單等所有依賴 client IP 的邏輯。
# 別名：未設 TI_ 前綴版時，沿用 uvicorn 生態慣用名 FORWARDED_ALLOW_IPS。
FORWARDED_ALLOW_IPS = os.getenv(
    "TI_FORWARDED_ALLOW_IPS", os.getenv("FORWARDED_ALLOW_IPS", "127.0.0.1")
)


def forwarded_allow_ips() -> str:
    """回傳啟動用的 forwarded_allow_ips；含 "*" 一律拒啟動（fail-closed）。

    安全設定取 fail-closed：寧可在啟動時明確報錯給出正確寫法，也不讓服務帶著
    「信任全部來源」的危險設定默默上線。空字串退回安全預設 "127.0.0.1"。
    屬「啟動時固定」設定（同 HOST/PORT），刻意不納入 reload()。
    """
    raw = (FORWARDED_ALLOW_IPS or "").strip()
    if not raw:
        return "127.0.0.1"
    if "*" in {item.strip() for item in raw.split(",")}:
        raise SystemExit(
            "TI_FORWARDED_ALLOW_IPS 嚴禁 '*'（會讓任何來源都能偽造 X-Forwarded-For）：\n"
            "請改列負載平衡器／反向代理的私網 IP 或 CIDR，例如\n"
            "  TI_FORWARDED_ALLOW_IPS=10.0.0.0/8,172.16.0.0/12,192.168.0.0/16"
        )
    return raw


# --- 路徑 ---------------------------------------------------------------
# PROJECT_ROOT 定義於檔案頂部（load_dotenv 之前，兩者共用單一來源）。


def env_path() -> str:
    """持久化設定 / 秘密的 .env 路徑（settings 與 auth 共用此單一來源）。"""
    return str(PROJECT_ROOT / ".env")


# 自訂角色檔目錄（roles/*.md，Markdown＋YAML frontmatter，一檔一角色；格式與載入規則見
# studio/role_store.py）。內建 8 角色為預設、同 key 檔案覆蓋；目錄不存在＝純內建行為。
ROLES_DIR = Path(os.getenv("TI_ROLES_DIR", str(PROJECT_ROOT / "roles")))

WORKSPACE_ROOT = Path(os.getenv("TI_WORKSPACE_ROOT", str(PROJECT_ROOT / "workspaces")))
HISTORY_ROOT = Path(os.getenv("TI_HISTORY_ROOT", str(PROJECT_ROOT / "history")))
# 跨場次教訓庫持久化檔（見 LESSONS_ENABLED）。預設置於專案根，已列入 .gitignore，不進版控。
LESSONS_FILE = Path(os.getenv("TI_LESSONS_FILE", str(PROJECT_ROOT / "lessons.json")))
# 考核庫持久化檔（見 APPRAISAL_ENABLED）。預設置於專案根，已列入 .gitignore，不進版控。
APPRAISALS_FILE = Path(os.getenv("TI_APPRAISALS_FILE", str(PROJECT_ROOT / "appraisals.json")))
WEB_DIR = PROJECT_ROOT / "web"

# Claude 訂閱 OAuth 憑證檔（claude CLI 登入後寫入，SDK 子程序沿用；claude_usage 讀其
# accessToken 查官方額度）。預設 ~/.claude/.credentials.json；可用 env 覆寫（測試隔離）。
CLAUDE_CREDENTIALS_FILE = Path(
    os.getenv("TI_CLAUDE_CREDENTIALS_FILE", str(Path.home() / ".claude" / ".credentials.json"))
)

# --- Claude 訂閱雙帳號自動輪替（決策純函式在 claude_accounts.pick_account、執行點在
# autopilot 主迴圈）。ROTATE=0 整段停用；PREFERRED 為主帳號 label（負載同分時優先）。
# 策略為「負載平均分配」：帳號負載＝5h/7d 兩額度窗用量取最大，主動把用量攤平到各帳號——
# 在線帳號比最低負載帳號高出 MARGIN（遲滯，%，避免頻繁重啟）即切換；THRESHOLD 為
# 安全上限（%）：在線帳號負載達此值一律強制切到仍低於上限的帳號。另納入重置時間（早重置
# 多吃：早歸還的額度先吃掉才不浪費；晚重置的要背久）：候選中「最早 7d 重置」者比次早者早
# ≥ RESET_EDGE_7D（秒，預設 6h）優先切給它——7d 窗是週尺度的稀缺資源，優先於「最早 5h
# 重置」早 ≥ RESET_EDGE（秒）的日內節奏規則。優先序：安全上限 > 7d 早重置 > 5h 早重置 >
# 負載平衡（v4，SSOT 見 pick_account docstring）。六者皆納入 reload()（UI 改 .env 後即時生效）。
CLAUDE_ROTATE = os.getenv("TI_CLAUDE_ROTATE", "1") not in ("0", "false", "False", "")
CLAUDE_ACCOUNT_PREFERRED = os.getenv("TI_CLAUDE_ACCOUNT_PREFERRED", "B")
CLAUDE_ROTATE_THRESHOLD = _env_float("TI_CLAUDE_ROTATE_THRESHOLD", 95.0)
CLAUDE_ROTATE_MARGIN = _env_float("TI_CLAUDE_ROTATE_MARGIN", 10.0)
CLAUDE_ROTATE_RESET_EDGE = _env_float("TI_CLAUDE_ROTATE_RESET_EDGE", 900.0)
CLAUDE_ROTATE_RESET_EDGE_7D = _env_float("TI_CLAUDE_ROTATE_RESET_EDGE_7D", 21600.0)
# scoped 週限（如 PM 釘的 Fable）救援輪替：在線帳號該模型 scoped 撞 CLAUDE_SCOPED_LIMIT_THRESHOLD
# 而對方仍有餘時,切過去讓 Fable 恢復可用（免全被改派較貴的 opus）。設 0 關閉、回退純全域決策。
CLAUDE_ROTATE_SCOPED = os.getenv("TI_CLAUDE_ROTATE_SCOPED", "1") not in ("0", "false", "False", "")

# Antigravity（agy）的 OAuth token 檔（agy 登入後寫入、執行時刷新；antigravity_usage 讀其
# token.access_token 查 Google Code Assist 配額）。預設 ~/.gemini/antigravity-cli/...；可 env 覆寫。
ANTIGRAVITY_OAUTH_TOKEN_FILE = Path(
    os.getenv(
        "TI_ANTIGRAVITY_OAUTH_TOKEN_FILE",
        str(Path.home() / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"),
    )
)

# --- 歷史 / 工作區保留（GC，避免自托管長跑下 history/ 與 workspaces/ 只增不減）----
# 每次 session 結束（finish_session）順手做一次輕量回收：刪掉「非 running」且超量/過舊的
# session（含 meta、events 與其 workspace 產出）。running 中的 session 一律保留。
# 兩規則取聯集（任一超標即回收）；設 0 = 該規則停用。MAX_COUNT 預設啟用（夠寬、足以保留
# 近期歷史，又能封住無上限成長）；MAX_AGE 預設停用（opt-in）。設 TI_HISTORY_MAX_COUNT=0
# 即完全還原成不自動回收。屬「啟動時固定」、非 UI 可調，故不納入 config.reload()。
HISTORY_MAX_COUNT = int(os.getenv("TI_HISTORY_MAX_COUNT", "200"))  # 最多保留幾個非 running session
HISTORY_MAX_AGE = int(os.getenv("TI_HISTORY_MAX_AGE", "0"))  # 最後活動超過幾秒即回收（0=停用）

# --- 伺服器 -------------------------------------------------------------
HOST = os.getenv("TI_HOST", "0.0.0.0")
PORT = int(os.getenv("TI_PORT", "8000"))

# 同時進行的討論場次上限（每場會起多個專家子程序 / LLM 連線；無上限時大量並發連線可
# 耗盡資源與 API 額度）。超過時新的 /ws 連線被拒（送 error 後 close 1013）。0 = 不限
# （向後相容）。預設給一個寬鬆上限，封住「大量分頁／腳本狂開連線」的失控情況。
MAX_CONCURRENT_SESSIONS = int(os.getenv("TI_MAX_CONCURRENT_SESSIONS", "8"))

# --- Autopilot（自主自我改善迴圈，由獨立的 ti-autopilot.service 跑）-------
# 持久任務 backlog 與狀態存這裡；working clone 與部署目標分開（避免改到正在跑的服務）。
AUTOPILOT_STATE_DIR = Path(os.getenv("TI_AUTOPILOT_STATE_DIR", str(PROJECT_ROOT / "autopilot")))
AUTOPILOT_WORK_DIR = Path(os.getenv("TI_AUTOPILOT_WORK_DIR", "/opt/ti-autopilot-work"))
AUTOPILOT_DEPLOY_DIR = Path(os.getenv("TI_AUTOPILOT_DEPLOY_DIR", str(PROJECT_ROOT)))
AUTOPILOT_REPO = os.getenv("TI_AUTOPILOT_REPO", "x812033727/Ti")  # owner/repo
# 主核心 repo：Ti 框架本身。專案討論中判定「需改 Ti 核心」的改動一律路由到這裡、開獨立 PR
# （絕不混入專案 repo）。固定綁定 AUTOPILOT_REPO，確保「路由目標」恆等於「autopilot 實際實作
# 並發佈核心改動的 repo」——兩者不可分歧（見 ARCHITECTURE.md「專案 repo 與 Ti 主核心 repo」）。
CORE_REPO = AUTOPILOT_REPO
AUTOPILOT_BRANCH = os.getenv("TI_AUTOPILOT_BRANCH", "main")  # 部署分支
AUTOPILOT_SERVICE = os.getenv("TI_AUTOPILOT_SERVICE", "ti.service")  # 重佈時要 restart 的服務
AUTOPILOT_HEALTH_URL = os.getenv("TI_AUTOPILOT_HEALTH_URL", "http://127.0.0.1:8021/api/health")
AUTOPILOT_COOLDOWN = int(os.getenv("TI_AUTOPILOT_COOLDOWN", "30"))  # 任務間最小喘息（秒）
AUTOPILOT_TASK_TIMEOUT = int(os.getenv("TI_AUTOPILOT_TASK_TIMEOUT", "3600"))
# 任務執行中「活動停滯」自癒門檻（秒）：連續此秒數「無進展」（events 檔 mtime 未前進 **且**
# worker 子程序零 CPU 活性）＝疑似子程序死鎖（非任務太大），autopilot 就地取消該場並標 failed
# 交分診自動重試，無需外部監控/重啟。以 events＋CPU 雙訊號判活（events 在長 inter-message 間隔
# 會凍結，CPU 兜底）。不變量：須 > TURN_HARD_TIMEOUT（單一 turn 可合法靜默的上限，預設 1800），
# 否則會誤殺慢但活著的 turn。預設 2400（40min）＝ TURN_HARD_TIMEOUT 之上留 600s headroom，
# 且 < AUTOPILOT_TASK_TIMEOUT(3600) 以更早自癒。0＝停用（退回僅硬牆逾時）。
AUTOPILOT_STALL_TIMEOUT = int(os.getenv("TI_AUTOPILOT_STALL_TIMEOUT", "2400"))

# AUTOPILOT_TIMEOUT_AUTOSPLIT：硬牆逾時任務（多半範圍太大跑不完）不再無聲 parked 等人工拆，而是交
#   資深專家自動拆成數個更小、可獨立出貨的子任務再排回 backlog、原任務歸檔 parked（完成率第二輪修法
#   ⑤，對治 7200s 硬牆下的 parked 死水）。0＝關，恢復舊行為（僅 parked 待人工）。以 split_depth 逐代
#   計數＋MAX_DEPTH 封頂,根治「子任務又太大又逾時、無限自我拆分」的爆炸。
AUTOPILOT_TIMEOUT_AUTOSPLIT = os.getenv("TI_AUTOPILOT_TIMEOUT_AUTOSPLIT", "1") not in (
    "0",
    "false",
    "False",
    "",
)
# 自動拆分的最大代數：任務帶 split_depth（拆分產物 = 父 depth+1）；達此上限的逾時任務不再自動拆、
#   維持 parked 待人工，避免無限拆分。2＝原任務可被拆一次、其子任務再逾時可再拆一次,之後止步。
AUTOPILOT_SPLIT_MAX_DEPTH = int(os.getenv("TI_AUTOPILOT_SPLIT_MAX_DEPTH", "2"))
# 單次拆分最多產出的子任務數（過濾/去重後再截斷），避免一次灌太多。
AUTOPILOT_SPLIT_MAX_SUBTASKS = int(os.getenv("TI_AUTOPILOT_SPLIT_MAX_SUBTASKS", "4"))

# 本工作室長期目標（北極星）：注入 autopilot 自評與 improver「找問題」的 discovery prompt，
# 讓自主提案可追溯到一致的長期方向（單一真相在此，消費端一律讀 config）。空字串＝不注入。
AUTOPILOT_NORTH_STAR = os.getenv(
    "TI_AUTOPILOT_NORTH_STAR",
    "持續提升 Ti 程式品質；強化 agent 間與跨 provider 溝通協作效能",
)
# 單一任務客觀閘門（lint/collect/test/merge）失敗時，重試同一任務的最大嘗試次數。
# 達上限才標 failed；避免每次失敗就 spawn 一個措辭近似的「修復X」新任務造成 backlog 暴增。
AUTOPILOT_TASK_MAX_ATTEMPTS = int(os.getenv("TI_AUTOPILOT_TASK_MAX_ATTEMPTS", "3"))
# 「討論未達完成且不可出貨」時重試同一任務的最大嘗試次數（預設 2，刻意 < 客觀閘門的 3）。
# 討論未收斂常是暫時性的（turn timeout 讓 QA 文字缺通過字樣、provider 抖動、單一 wave
# flaky、critic 一時否決；LLM 非決定性，重跑常會過），值得有限次重試而非單發即永久 failed
# ——那是完成率最大的失敗桶（見完成率診斷）。但每次重試燒一整場 1–4h session，需設上限
# 避免對真的不可收斂任務空耗額度。達上限才標 failed（note 仍含「討論未達完成」）。
# 預設 2→3（第五輪 C1）：實測 failed 23 筆中 48% 是此桶且 cap=2 擋死；LLM 非決定性，
# 第 3 次常會過——與客觀閘門上限對齊，成本由每日 PR 預算兜底。
AUTOPILOT_DISCUSSION_MAX_ATTEMPTS = int(os.getenv("TI_AUTOPILOT_DISCUSSION_MAX_ATTEMPTS", "3"))
# 額度感知節奏（quota gate）：主迴圈取任務前先查 provider 額度快照（provider_quota.snapshot
# ＋ gate()），全部 provider 受限（未就緒/查詢異常/用量達門檻）時睡到最早重置再重查，取代
# 「額度耗盡仍空轉把任務燒成 failed」。GATE=0 可關閉（維持舊行為）；MAX_SLEEP 為單次睡眠
# 上限秒數（防 reset 資訊異常導致睡過頭，醒來會重查快照再決定）。
AUTOPILOT_QUOTA_GATE = os.getenv("TI_AUTOPILOT_QUOTA_GATE", "1") not in ("0", "false", "False", "")
AUTOPILOT_QUOTA_MAX_SLEEP = int(os.getenv("TI_AUTOPILOT_QUOTA_MAX_SLEEP", "1800"))
# 每日 PR 成本熔斷：UTC 當日 autopilot 實際開出的 PR 數（audit.jsonl 中 pr 非空的紀錄）達
# 上限即停止接新任務，睡到跨日自動恢復（不寫 pause 檔、免人工 resume）。0＝不限制（預設，
# 行為不變）。防幻覺任務迴圈在單日燒光 PR/CI/LLM 成本。
AUTOPILOT_DAILY_PR_BUDGET = int(os.getenv("TI_AUTOPILOT_DAILY_PR_BUDGET", "0"))
# 任務開場前的 PM workflow 分診：小任務走「快速模式」省三審輪次、高風險走「預設流程」完整
# 把關（一次 MODEL_FAST 級短呼叫）。預設關閉（0）：維持「autopilot 一律走 default_workflow
# 安全骨架」的既有不變式，opt-in 後才生效；任何分診失敗（LLM 錯誤/逾時/非法名稱）都退回
# 預設流程。TRIAGE_TIMEOUT 為單次分診呼叫的硬逾時（秒），由 complete_once 吞掉不外洩。
AUTOPILOT_WORKFLOW_TRIAGE = os.getenv("TI_AUTOPILOT_WORKFLOW_TRIAGE", "0") not in (
    "0",
    "false",
    "False",
    "",
)
AUTOPILOT_TRIAGE_TIMEOUT = int(os.getenv("TI_AUTOPILOT_TRIAGE_TIMEOUT", "60"))
# provider 額度快照 SWR（stale-while-revalidate）：provider_quota.snapshot() 的模組級快取
# 過期後，只要舊快照年齡未超過此秒數，就立即回舊快照（附 stale=true）並由背景執行緒刷新，
# 讓設定面板、orchestrator 派工與 autopilot 額度閘門等關鍵路徑不必同步等最慢 provider。
# 設 0 停用 SWR（快取一過期就同步查，回到舊行為）。
QUOTA_STALE_MAX = _env_float("TI_QUOTA_STALE_MAX", 300.0)
# 軟性時間預算：session 在硬 timeout（AUTOPILOT_TASK_TIMEOUT，由 autopilot 的 wait_for 套用）的
# 此比例處主動收斂——停止派發新任務、把已完成的走 Demo/出貨、未動的記 known-limit/followup，
# 換取「優雅收尾並回傳結果」而非被 wait_for 硬砍、整場(含已完成任務)全丟成 timeout failed。
# 預設 0.85（留 15% 給 Demo/發佈/wrap-up）。只在 autopilot 傳入 time_budget_s 時生效。
SESSION_SOFT_DEADLINE_FRAC = float(os.getenv("TI_SESSION_SOFT_DEADLINE_FRAC", "0.85"))
# 每場用量預算（成本熔斷）：與時間預算同機制——session 累計用量達上限即停止派發新任務、優雅收尾
# 出貨，治「失控場一路燒 token 到撞硬 timeout」。兩者皆 0＝不限（預設不啟用，維持既有行為）；
# TOKEN 為單場 total token 上限、USD 為單場估算成本上限（採事件回報的 cost_usd 累計）。
SESSION_TOKEN_BUDGET = int(os.getenv("TI_SESSION_TOKEN_BUDGET", "0"))
SESSION_USD_BUDGET = float(os.getenv("TI_SESSION_USD_BUDGET", "0"))
# 部署 idle 守衛的 stale 門檻（秒）：status 卡在 running 但最後活動超過此值的討論視為死掉、
# 不再算「進行中」，避免崩潰沒收尾的 session 永久擋住 autodeploy / autopilot 重佈。預設 30 分。
DEPLOY_STALE_AFTER = int(os.getenv("TI_DEPLOY_STALE_AFTER", "1800"))
AUTOPILOT_PAUSE_FILE = Path(
    os.getenv("TI_AUTOPILOT_PAUSE_FILE", str(PROJECT_ROOT / "AUTOPILOT_PAUSED"))
)
# auto 派工模式哨兵檔：存在＝auto（PM 全權派工）、不存在＝手動（現行規則裁決）。
# 與 pause 檔同機制——web 端點寫/刪、orchestrator 每場 session 開頭重讀，跨程序免重啟生效。
DISPATCH_AUTO_FILE = Path(os.getenv("TI_DISPATCH_AUTO_FILE", str(PROJECT_ROOT / "DISPATCH_AUTO")))
AUTOPILOT_DRYRUN = os.getenv("TI_AUTOPILOT_DRYRUN", "0") not in ("0", "false", "False", "")
# 推送/合併安全旗標（預設皆取安全側）：
# AUTOPILOT_FORCE_PUSH：預設非強制推送；遠端已存在同名分支會中止。設 1 才略過中止並改用
#   `git push --force-with-lease --force-if-includes`（覆寫殘留分支用，絕不用裸 -f）。
AUTOPILOT_FORCE_PUSH = os.getenv("TI_AUTOPILOT_FORCE_PUSH", "0") not in ("0", "false", "False", "")
# AUTOPILOT_RECLAIM_BRANCH：遠端殘留同名任務分支（前次執行在「等 CI→合併」期間被中斷而留下）
#   不再一律中止，改自動認領：殘留 open PR 先關閉、殘留分支刪除，然後照常推送。分支名由
#   task id 決定，殘留必屬同一任務的前次執行，認領不會動到別的任務；零 diff 檢查在認領之前，
#   「前次已合併、本次無新內容」會先收斂成 no_changes，認領不會覆蓋已合併成果。與 FORCE_PUSH
#   的差異：FORCE_PUSH 是覆寫同一分支（force push），認領是刪舊開新、不需 force。
#   設 0 恢復「偵測到殘留即中止」的舊行為（任務會被自己的殘留永久擋死，需人工清理）。
AUTOPILOT_RECLAIM_BRANCH = os.getenv("TI_AUTOPILOT_RECLAIM_BRANCH", "1") not in (
    "0",
    "false",
    "False",
    "",
)
# PUBLISH_BYPASS_INFRA_CI：自動合併等 CI 時，若 CI「失敗」其實是基礎設施/帳務秒掛
#   （所有失敗 check 在數秒內 conclusion=failure、零步驟執行，如 GitHub Actions 命中
#   spending limit）而非程式碼問題，則繞過此「等 CI→紅就保留待人工」的自設閘直接合併。
#   安全前提：合併目標分支未受保護（受保護時合併仍會被 GitHub 擋下、自然 fall back），
#   且 autopilot 發佈前的 sandbox pytest+lint 已驗過碼。預設開啟；設 0 關閉、回到 CI 紅就待人工。
PUBLISH_BYPASS_INFRA_CI = os.getenv("TI_PUBLISH_BYPASS_INFRA_CI", "1") not in (
    "0",
    "false",
    "False",
    "",
)
# 判定「秒掛」的單一 check 最長執行秒數門檻：所有失敗 check 皆短於此值才視為基礎設施問題
#   （真實 lint/test 光 checkout＋setup 就遠超此值）。任一失敗 check 超門檻＝可能真失敗→不繞過。
PUBLISH_INFRA_CI_MAX_SECONDS = _env_float("TI_PUBLISH_INFRA_CI_MAX_SECONDS", 25.0)
# AUTOPILOT_PROTECTION_CHECK：第二道防線——squash-merge 前主動查合併目標（AUTOPILOT_BRANCH）
#   的分支保護狀態，「無法確認（403 無權限／網路／逾時）」一律 fail-safe 中止。預設啟用；
#   無 Administration:read 權限而每次卡在「無法確認」的環境，可設 TI_AUTOPILOT_PROTECTION_CHECK=0
#   整段跳過（明確逃生口）。
AUTOPILOT_PROTECTION_CHECK = os.getenv("TI_AUTOPILOT_PROTECTION_CHECK", "1") not in (
    "0",
    "false",
    "False",
    "",
)
# AUTOPILOT_DEPLOY_CHECK_INTERVAL：任務邊界部署漂移自查（完成率第三輪修法二A）。autodeploy
#   timer 只在「無進行中討論」時 pull+restart——autopilot 連續跑任務時討論幾乎總在進行，部署
#   窗口極少，已合併的修法會長時間「紙上上線」（execv 自我重載也要磁碟碼先變，雞生蛋）。
#   主迴圈在任務邊界（此刻保證無 autopilot 討論）每隔此秒數 fetch 比對 origin/<branch>，
#   有 drift 且無手動討論即就地 deploy.redeploy()＋execv 重載。0＝關閉（回舊行為，只靠 timer；
#   tests/conftest.py 對整個測試樹設 0——此檢查會真的 fetch/reset/restart，絕不可在測試觸發）。
AUTOPILOT_DEPLOY_CHECK_INTERVAL = int(os.getenv("TI_AUTOPILOT_DEPLOY_CHECK_INTERVAL", "300"))
# 邊界重佈失敗（已自動回滾）後的退避秒數：避免壞 commit 讓每輪任務邊界都白燒一次 redeploy；
# autodeploy timer 原邏輯仍在，雙保險。
AUTOPILOT_DEPLOY_FAIL_BACKOFF = int(os.getenv("TI_AUTOPILOT_DEPLOY_FAIL_BACKOFF", "1800"))
# AUTOPILOT_AUTO_MERGE：開 PR 後掛 GitHub 原生 auto-merge（完成率第三輪修法二B）。舊同步
#   路徑阻塞等 CI（PUBLISH_CI_TIMEOUT=600s）：被中斷＝殘留 open PR＋任務被自己的殘留擋死；
#   CI 慢於 600s＝關 PR 丟掉整份成品。掛 auto-merge 後短窗輪詢（AUTOPILOT_MERGE_FAST_WAIT），
#   窗滿任務標 merging 續跑下一場，由主迴圈 reconciler（每 15 分鐘）收斂：MERGED→done、
#   BEHIND→update-branch（main 保護 strict:true 的必要配套）、CI 紅/衝突→關 PR 退回、
#   逾齡（AUTOPILOT_MERGE_MAX_AGE）→退回重排；並認領/清理孤兒 autopilot PR。
#   需 repo 開 Allow auto-merge（已確認開啟）。設 0＝完全回到同步等 CI 舊路徑。
AUTOPILOT_AUTO_MERGE = os.getenv("TI_AUTOPILOT_AUTO_MERGE", "1") not in (
    "0",
    "false",
    "False",
    "",
)
# auto-merge 掛上後的短窗輪詢秒數（多數 CI 幾分鐘內綠，窗內合併＝與舊成功路徑等價）；
# 0＝掛上即走（任務直接標 merging）。
AUTOPILOT_MERGE_FAST_WAIT = int(os.getenv("TI_AUTOPILOT_MERGE_FAST_WAIT", "180"))
# merging 任務等待背景合併的最長秒數：逾齡由 reconciler 關 PR 退回重排（note 帶「逾時」
# 命中 INFRA_FAILURE_RE，triage 可自動重排）。
AUTOPILOT_MERGE_MAX_AGE = int(os.getenv("TI_AUTOPILOT_MERGE_MAX_AGE", "7200"))
# AUTOPILOT_EVAL_MEMORY：自我評估時回饋「近期成敗」給資深專家的筆數（每類 done/failed 各取
#   最近 N 筆）。讓評估記取自身成績單——避免重提已完成、避開已知失敗做法，越跑越聚焦。
#   0 = 停用（還原成無狀態評估）。
AUTOPILOT_EVAL_MEMORY = int(os.getenv("TI_AUTOPILOT_EVAL_MEMORY", "20"))
# LINT_AUTOFORMAT：lint 閘門遇「機器可修項」失敗時先自動修再重驗，重驗綠即視同通過
#   （機器可確定性修復的問題不值得把整場 1-2 小時的討論退回重試；見任務 #249 卡格式牆、
#   #496/#364/#367 卡 import 排序）。涵蓋兩類：
#   - `ruff format --check` 紅 → `ruff format` 寫回重驗（純排版漂移）。
#   - `ruff check` 紅 → `ruff check --fix`（僅 safe 修正，如 I001 import 排序、F401 未用
#     import）寫回重驗；E402 等非 safe-fixable 規則修不掉、照舊退回。
#   任一重驗仍紅維持原退回行為。預設開啟；設 0 恢復舊行為（任一 ruff 紅即退、絕不寫回）。
LINT_AUTOFORMAT = os.getenv("TI_LINT_AUTOFORMAT", "1") not in ("0", "false", "False", "")

# AUTOPILOT_DEDUP_RATIO：自我評估「提案進場」前，用詞集 Jaccard 相似度（autopilot._token_set_similarity，
#   ASCII 片段整段 + CJK 逐字，純 stdlib 無新依賴）對每個提案與目前 pending/in_progress 標題算相似度，
#   ≥ 此閾值即視為實質重疊、丟棄（記 log.debug）。閾值收斂為單一純模組常數、不開 env override，
#   日後調整改此處一個值即可。
#   0.75 為實測定值：詞集策略在同一 0.75 下已能多攔「語序調換」改寫（SequenceMatcher 在 0.75 漏網的
#   案例 Jaccard 可達 1.0），且不誤殺「相反意圖但詞集高重疊」的合法不同任務（Jaccard≈0.556 < 0.75）。
#   調低到 0.55 經實測：對應重複樣本無新增命中、反而會誤殺「同領域但語意相反」哨兵案例（如「提高重試
#   上限」↔「降低重試上限」詞集高重疊），故不採——治理「同主題反覆疊加」隧道效應的主防線是子系統
#   覆蓋計數器（另案），本閾值僅補進場語意去重。無共享字根的純同義替換（如「補」↔「新增」）詞集仍
#   擋不住，誠實標為 known-limitation（見 test_autopilot_synonym_dedup.py / test_autopilot_prefilter.py）。
#   僅作用於本次提案進場，不回溯刪改 backlog、不動 backlog 既有字串等值去重契約。
AUTOPILOT_DEDUP_RATIO = 0.75

# AUTOPILOT_PREFILTER_IMPLEMENTED：任務 pick 前的「疑似已實作」預篩總開關（#2 接線使用）。
# 語料來自近期 merged PR 標題，無 token 或 API 不可用時退回本地 git log。命中後不丟棄任務，
# 只降級走既有 investigation lane；因此這裡只放可即時調整的判定旋鈕。
AUTOPILOT_PREFILTER_IMPLEMENTED = os.getenv("TI_AUTOPILOT_PREFILTER_IMPLEMENTED", "1") not in (
    "0",
    "false",
    "False",
    "",
)
AUTOPILOT_PREFILTER_RATIO = _env_float("TI_AUTOPILOT_PREFILTER_RATIO", 0.80)
AUTOPILOT_PREFILTER_LOOKBACK_DAYS = _env_int("TI_AUTOPILOT_PREFILTER_LOOKBACK_DAYS", 60)

# AUTOPILOT_SUBSYSTEM_MAX_PENDING：自我評估「提案進場」的第二道（廣度）防線 K。從標題以 regex 抽出
#   「涉及子系統」（_extract_subsystems），若某子系統在現有 pending/in_progress 已達 K 筆，該子系統的
#   新提案一律拒——避免 LLM 不換標題卻反覆對同一模組（backlog、discovery…）疊加任務（topic echo
#   chamber）。純模組常數、零 env、零新依賴。僅作用於本次提案進場：不回溯刪改 backlog、不動
#   `backlog._is_duplicate` 的字串等值去重契約（與第一道 difflib 相似度防線互補）。3 為初始估值
#   （同一子系統最多排 3 筆，第 3 筆起的新提案被擋），日後調整改此處一個值即可。
AUTOPILOT_SUBSYSTEM_MAX_PENDING = 3

# AUTOPILOT_SUBSYSTEM_MAX：discovery prompt 的「已過多子系統」軟提示門檻。同一子系統（_extract_subsystems
#   識別）在 pending/in_progress 達此筆數，prompt 就主動把它列出，提示 LLM 生成階段繞開、逼出主題廣度。
#   與 _MAX_PENDING 互補分層：本常數是「軟引導」（prompt 早一步提醒，預設 2），_MAX_PENDING 是進場「硬擋」
#   （pre-filter 拒收，預設 3）。單一純模組常數、無 env override，日後調整改此處一個值即可。
AUTOPILOT_SUBSYSTEM_MAX = 2

# 不變式：軟提示門檻必須嚴格小於硬擋門檻，否則 prompt 還沒提醒就已被 pre-filter 硬拒，
# 非對稱「軟引導早一步、硬擋兜底」的分層設計會靜默失效。任何人調高 SUBSYSTEM_MAX 而忘改
# MAX_PENDING 會在 import config 時即時炸出，攔在最便宜的階段（成本一行、防護真實）。
assert AUTOPILOT_SUBSYSTEM_MAX < AUTOPILOT_SUBSYSTEM_MAX_PENDING, (
    "AUTOPILOT_SUBSYSTEM_MAX（軟提示門檻）必須 < AUTOPILOT_SUBSYSTEM_MAX_PENDING（進場硬擋門檻）"
)

# AUTOPILOT_FOLLOWUP_VALUE_GATE：discovered followup 進場的「良構性/價值閘」（完成率第二輪診斷）。
#   前輪三大失敗桶（討論不收斂/lint/零-diff）皆為症狀，真正上游是 autopilot 自我衍生的「收尾驗收/
#   權威證據檔/closure 報告/sha256 落檔/重跑並回報」這類**無會改動程式碼的客觀完成判準**的自我指涉
#   meta 任務——它們同時灌三桶（ill-posed 討論永不收斂、生成檔 from __future__/E402 lint 修不掉、零-diff
#   merge）。去重防線（F 的 _filter_pending_duplicates）擋不掉「全新但同樣沒價值」的 busywork（例：定義
#   body_sha256 的 jq 換行慣例）。此閘是 F 的延伸：在 _screen_followups 內加第三道，對命中「證據儀式」
#   訊號**且**不含任何實作/修復/重構等會改碼動詞的提案，於進場時丟棄。刻意採「命中 busywork 訊號 AND
#   缺 code-work 訊號」的雙條件（見 autopilot._is_low_value_followup）：任何實作/測試/守門訊號即豁免保留，
#   偏向「寧可放行、不誤殺」的保守方向。純標題/detail 正則、stdlib-only、僅作用於本次進場，不回溯刪改
#   backlog、不動字串等值去重契約。預留 env kill-switch（實測若在 prod 誤殺可即時關）。
AUTOPILOT_FOLLOWUP_VALUE_GATE = os.getenv("TI_AUTOPILOT_FOLLOWUP_VALUE_GATE", "1") not in (
    "0",
    "false",
    "False",
    "",
)

# AUTOPILOT_INVESTIGATION_LANE：調查/驗證型任務分流輕量管線（完成率第三輪修法一）。
#   驗屍 14 筆「討論未達完成」failed：9 筆是純調查/驗證/證據儀式型——這類任務的正確完成判準
#   是「產出結構化結論」而非「code 過三審＋Demo」，卻被送進多專家全套管線：工程師把結論落檔
#   到自己 sandbox 的 $TMPDIR、QA 換 shell 讀不到 → 每輪 FAIL 同因，結構上不可能過；researcher
#   實際已產出高品質結論但整場被判 failed 全數丟棄，再被有限重試重燒一場（每場 ~100 分鐘）。
#   開啟時（預設）：命中調查訊號且無 code-work 豁免訊號的任務改走單專家調查 → 結構化結論寫回
#   backlog note＋教訓庫，不進 StudioSession 多專家討論、不經 lint/collect/test/merge 閘門。
#   誤分類安全閥：專家輸出 `需改碼:` 即退回完整管線重跑（不消耗 attempts）。設 0 回復現行為。
AUTOPILOT_INVESTIGATION_LANE = os.getenv("TI_AUTOPILOT_INVESTIGATION_LANE", "1") not in (
    "0",
    "false",
    "False",
    "",
)
# 調查管線單次專家呼叫的硬逾時（秒）：遠小於整場 session 的 AUTOPILOT_TASK_TIMEOUT(3600)——
# 輕量管線就該輕量，逾時走「討論未達完成」既有重試語意。
AUTOPILOT_INVESTIGATION_TIMEOUT = int(os.getenv("TI_AUTOPILOT_INVESTIGATION_TIMEOUT", "1200"))
# 調查旁路併行(吞吐強化 δ):主 worker 跑完整管線時,背景線併行消化調查分流任務
# (live 量測 pending 37% 符合、每筆 ~89s vs 完整管線 ~51min)。單線+與主迴圈共用
# pause/quota 閘門+原子認領+獨立唯讀 clone。**預設 0 灰度**,穩定後翻 1。
AUTOPILOT_INVESTIGATION_PARALLEL = os.getenv("TI_AUTOPILOT_INVESTIGATION_PARALLEL", "0") not in (
    "0",
    "false",
    "False",
    "",
)
# AUTOPILOT_INVESTIGATION_REFUTE：調查結論的對抗性驗證（refuter）。單專家調查的已知風險是
#   「自說自話」——結論寫得頭頭是道、證據卻對不上（reward hacking），而結論會進教訓庫污染
#   長期記憶。開啟時（預設）結論標 done 前多一次廉價 MODEL_FAST 呼叫（providers.complete_once，
#   永不 raise），專職試圖推翻：推得翻 → 不標 done，走「討論未達完成」重試（note 帶破綻）；
#   推不翻/refuter 壞掉/離線 → 照常 done（寧放勿殺，refuter 是加值防線不是依賴）。
#   調查任務量低、每筆只多一次 FAST 呼叫，額度成本可忽略。設 0 關閉。
AUTOPILOT_INVESTIGATION_REFUTE = os.getenv("TI_AUTOPILOT_INVESTIGATION_REFUTE", "1") not in (
    "0",
    "false",
    "False",
    "",
)

# EXPERT_LINT_HOOK：寫時 lint——Claude 專家每次 Write/Edit .py 後（PostToolUse hook）與
#   OpenAI 相容專家 write_file/edit_file 後，自動 `ruff check --fix`（safe-only）＋
#   `ruff format`，殘餘違規以文字回饋讓專家當場修。治「lint 事後才紅」：#249/#496/#364/
#   #367 連續三輪各燒 1-2 小時只為空格（見 autopilot._gate_lint docstring）。fail-open：
#   非 .py／無 ruff（外部非 Python 專案）／逾時／例外一律靜默放行，絕不擋寫檔。設 0 關閉。
EXPERT_LINT_HOOK = os.getenv("TI_EXPERT_LINT_HOOK", "1") not in ("0", "false", "False", "")
# 寫時 lint 單一 ruff 命令的逾時秒數（每次寫檔最多三支命令：check --fix / format / check）。
EXPERT_LINT_TIMEOUT = float(os.getenv("TI_EXPERT_LINT_TIMEOUT", "15"))

# EXPERT_SKILLS：Claude 專家的 skills 漸進揭露(SKILL.md)——出貨自檢/調查輸出契約等程序
#   知識放 .claude/skills/,專家需要時經 Skill 工具載入,不占常駐 system prompt。
#   **預設 0(灰度)**:SDK 一設 skills 會把 setting_sources 從「全載」改窄(experts._skills_options
#   已顯式鎖 ["project"] 隔離 user/local 層),行為面有變化,先灰度觀察再翻 1。
#   只影響 Claude 專家(OpenAI/Codex/Antigravity 無 Skill 工具)。
EXPERT_SKILLS = os.getenv("TI_EXPERT_SKILLS", "0") not in ("0", "false", "False", "")
# 哪些角色啟用 skills(csv):預設寫碼/審查三角——PM/架構師不寫碼,researcher 用不到出貨程序。
EXPERT_SKILLS_ROLES = frozenset(
    r.strip()
    for r in os.getenv("TI_EXPERT_SKILLS_ROLES", "engineer,senior,qa").split(",")
    if r.strip()
)

# CONVENTIONS_CARD：專家慣例卡——把執行環境慣例（.venv/bin/python 強制、timeout 前綴、
#   別重複整檔重讀/重跑 git status、禁落檔 $TMPDIR）附進每位專家 system prompt 尾端，
#   cwd 是 Ti repo 時另附測試/lint/config 速查。治「慣例只寫在 CLAUDE.md 但專家沒被注入、
#   每場重教且不遵守」（實證：同場混用三種直譯器寫法、git status 64 次/場）。
#   內容見 studio/conventions.py（≤30 行硬上限）。設 0 完全關閉。
CONVENTIONS_CARD = os.getenv("TI_CONVENTIONS_CARD", "1") not in ("0", "false", "False", "")

# 專家推理深度(SDK effort,僅 Claude 路徑):全域預設+per-role 覆寫。預設兩者皆空=不傳
# (SDK 預設),零行為改變;要省額度時對審查/反思型角色降檔(如
# TI_EXPERT_EFFORT_MAP="security:low,architect:medium,oneshot:low")。合法值 low/medium/
# high/xhigh/max;非法值解析時略過並記 warning,不擋啟動。
VALID_EFFORT = ("low", "medium", "high", "xhigh", "max")
EXPERT_EFFORT = os.getenv("TI_EXPERT_EFFORT", "").strip().lower()


def _parse_effort_map(raw: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for pair in (raw or "").split(","):
        pair = pair.strip()
        if not pair:
            continue
        key, _, val = pair.partition(":")
        key, val = key.strip().lower(), val.strip().lower()
        if key and val in VALID_EFFORT:
            out[key] = val
        elif key:
            logging.getLogger("ti.config").warning(
                "TI_EXPERT_EFFORT_MAP 含非法 effort 值,已略過:%s", pair
            )
    return out


EXPERT_EFFORT_MAP = _parse_effort_map(os.getenv("TI_EXPERT_EFFORT_MAP", ""))


def effort_for(role_key: str) -> str | None:
    """該角色生效的推理深度:per-role map > 全域 > None(SDK 預設)。"""
    v = EXPERT_EFFORT_MAP.get((role_key or "").lower(), "") or EXPERT_EFFORT
    return v if v in VALID_EFFORT else None


# 專家閒置回收(效能強化 B1,僅 Claude 路徑):每個常駐專家=一個 SDK 子行程(RSS
# 270-500MB),lane×角色疊加即 6-12GB 峰值。閒置超過此秒數的專家由 session reaper 斷線
# 回收(client 重建,下次 speak 自動重連;對話脈絡歸零,由 NOTES/reflexion 補償)。
# **預設 0=完全關閉(零行為改變)**;生產建議 900。EXEMPT=豁免角色 csv(pm 脈絡最值錢)。
# 主迴圈心跳停滯告警秒數(β):非暫停且無任務執行中,主迴圈 tick 逾此秒數未推進即
# log.error(告警不自殺,自救交 systemd watchdog)。0=關。
AUTOPILOT_LOOP_STALL_S = int(os.getenv("TI_AUTOPILOT_LOOP_STALL_S", "900"))
# open PR reconciler 的節流間隔秒數(第五輪 P1):常駐背景線+任務邊界共用同一節流。
# 0=停用 reconciler(邊界+背景皆不跑)。舊值 900 且只在任務邊界跑,實測 merging 卡 2-8h。
AUTOPILOT_RECONCILE_INTERVAL_S = int(os.getenv("TI_AUTOPILOT_RECONCILE_INTERVAL_S", "300"))
# 主動通知 webhook（功能第五輪 F2）：異常事件（task_failed/loop_stall/quota_exhausted）
# POST JSON 到此 URL。空＝關（預設）。實作在 studio/notify.py（零依賴、失敗吞掉）。
NOTIFY_WEBHOOK = os.getenv("TI_NOTIFY_WEBHOOK", "").strip()
EXPERT_IDLE_STOP_S = int(os.getenv("TI_EXPERT_IDLE_STOP_S", "0"))
EXPERT_IDLE_STOP_EXEMPT = frozenset(
    r.strip().lower() for r in os.getenv("TI_EXPERT_IDLE_STOP_EXEMPT", "pm").split(",") if r.strip()
)


# AUTOPILOT_FOLLOWUP_MAX_PER_TASK：單一任務完成後，討論 discovered followup 的「扇出寬度」上限——
#   品質防線（去重 + 價值閘）後再截斷到此數。對治完成率診斷的「一個任務繁殖一堆 followup」echo
#   chamber：價值閘擋「沒價值的」、本上限擋「同源衍生太多的」，互補封住 discovered 迴圈灌水（修法②）。
#   3＝一場討論最多回填 3 個後續；0＝不限（恢復舊行為）。
AUTOPILOT_FOLLOWUP_MAX_PER_TASK = int(os.getenv("TI_AUTOPILOT_FOLLOWUP_MAX_PER_TASK", "3"))
# AUTOPILOT_FOLLOWUP_MAX_GEN：discovered followup 的「血緣代數」上限（seed/manual/eval=0，父任務的
#   followup=父+1）。父任務 gen 已達此上限時，其 discovered followup 一律不入場（留痕丟棄）——斷開
#   「followup 生 followup 生 followup」的深鏈,與寬度上限一縱一橫共同封頂 discovered 扇出爆炸。
#   3＝原始任務可衍生到第 3 代,之後止;0＝不限（恢復舊行為）。
AUTOPILOT_FOLLOWUP_MAX_GEN = int(os.getenv("TI_AUTOPILOT_FOLLOWUP_MAX_GEN", "3"))
# AUTOPILOT_DISCOVERED_DAILY_CAP：每日（UTC）自產任務入列總量上限（source=discovered/eval
#   合計）。價值閘擋「爛的」、寬度/代數閘擋「同源太多的」，此為總量閘——實測 pending 172
#   筆中 85% 自產、產生速度 > 消化速度（~8/天），存量只增不減。20＝寬鬆日額；0＝不限。
AUTOPILOT_DISCOVERED_DAILY_CAP = int(os.getenv("TI_AUTOPILOT_DISCOVERED_DAILY_CAP", "20"))
# AUTOPILOT_RETRY_COOLDOWN_S：討論未收斂退回 pending 後的重抓冷卻秒數（retry_after 欄位，
#   next_pending/claim_next 尊重）。0＝不冷卻（舊行為）。動機：2026-07-11 09:24 LLM 劣化
#   窗口,調查失敗退回後旁路 60s 即重抓,3 次 attempts 在 3 分鐘內於同一窗口內燒光——
#   冷卻把重試錯開,撐過短暫劣化。600＝重試間隔 10 分鐘,3 次橫跨 >20 分鐘。
AUTOPILOT_RETRY_COOLDOWN_S = int(os.getenv("TI_AUTOPILOT_RETRY_COOLDOWN_S", "600"))


# --- state 安全寫入（root-only chown 驗證）---------------------------------
def env_bool(name: str, default: bool) -> bool:
    """讀布林環境變數；未設／空字串／純空白退回 default。

    沿用本檔既有布林慣例——「假值」集合為 ("0","false","False")，其餘非空值皆為真；
    與 _env_float「空＝未設定」的容錯一致。提為 public 供 config 內外共用（單一真值來源）。
    """
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip() not in ("0", "false", "False")


# state 檔案（history meta/events、backlog.json）root-only 安全寫入的三態模式。
REQUIRE_CHOWN_MODES = ("strict", "warn", "off")


def _parse_require_chown() -> str:
    """解析 TI_REQUIRE_CHOWN → strict / warn / off，預設與無法辨識值皆 fail-safe 取 strict。

    - 未設／留空／"strict"：strict（安全側；預設路徑靜默，不記 warning）。
    - "warn"：降級為「chown 失敗只警告、不阻擋」，記「降級」warning。
    - "off" 或布林假值（0/false/False）：完全停用 owner 驗證，記「降級」warning。
    - 其餘無法辨識值：fail-safe 取 strict，記「無法辨識」warning。
    """
    raw = (os.getenv("TI_REQUIRE_CHOWN") or "").strip().lower()
    if not raw or raw == "strict":
        return "strict"
    if raw == "warn":
        logger.warning("TI_REQUIRE_CHOWN 降級至 warn：state 寫入 chown 失敗時僅警告、不阻擋")
        return "warn"
    # off 同義：複用 env_bool 慣例判布林假值，外加常見停用字 no（env_bool 假值集不含）。
    if raw in ("off", "no") or not env_bool("TI_REQUIRE_CHOWN", True):
        logger.warning("TI_REQUIRE_CHOWN 降級至 off：完全停用 state 寫入的 root owner 驗證")
        return "off"
    logger.warning("TI_REQUIRE_CHOWN=%r 無法辨識，fail-safe 取 strict", raw)
    return "strict"


# 模組頂層常數：import 時一次解析；importlib.reload(config) 即可切值（不需 cache 失效）。
REQUIRE_CHOWN = _parse_require_chown()


def require_chown_mode() -> str:
    """目前的 state 安全寫入模式（strict/warn/off）。回傳模組頂層 REQUIRE_CHOWN。

    只回傳常數、不重讀 env：語意清晰、無副作用，可被測試 monkeypatch 成 lambda。
    """
    return REQUIRE_CHOWN


# --- 專案（長期產品）與持續改良迴圈 ---------------------------------------
# 專案是跨 session 的一級實體：固定 workspace（程式碼與 git 歷史跨場次累積）、
# 專屬 backlog（改良任務佇列）。持續改良迴圈（improver）重複「取 backlog 任務 →
# 跑一場討論 → followups 回填 → backlog 空了就『找問題』產生新任務」，
# 讓團隊對同一個產品一直找問題、一直改良。
PROJECTS_ROOT = Path(os.getenv("TI_PROJECTS_ROOT", str(PROJECT_ROOT / "projects")))

# 持續改良「找問題」階段的視角（csv）：多視角並行審視產品再彙整去重——senior 看工程品質、
# pm 看用戶價值/功能缺口、researcher 上網看同類產品與最佳實踐。只在 backlog 清空時發生
# （低頻），多幾次呼叫可接受；設 "senior" 一鍵還原舊的單視角行為。
DISCOVER_ROLES = [
    r.strip()
    for r in os.getenv("TI_DISCOVER_ROLES", "senior,pm,researcher").split(",")
    if r.strip()
]
# 單次「持續改良」連線最多跑幾輪（每輪＝一場完整討論）；0 = 不限（直到找不到新改善點）。
# 預設給保守上限，避免一次連線燒掉過多 API 額度。
IMPROVE_MAX_CYCLES = int(os.getenv("TI_IMPROVE_MAX_CYCLES", "5"))
# 連續失敗幾輪即停（避免同一個壞任務無限重試空轉）。
IMPROVE_MAX_FAILS = int(os.getenv("TI_IMPROVE_MAX_FAILS", "2"))
# 每輪之間的喘息秒數（0 = 不等待）。
IMPROVE_COOLDOWN = float(os.getenv("TI_IMPROVE_COOLDOWN", "0"))


def autopilot_paused() -> bool:
    """暫停開關：pause 檔存在、或 env TI_AUTOPILOT_PAUSED 為真，即暫停迴圈。"""
    if os.getenv("TI_AUTOPILOT_PAUSED", "0") not in ("0", "false", "False", ""):
        return True
    return AUTOPILOT_PAUSE_FILE.exists()


def dispatch_auto() -> bool:
    """auto 派工模式開關：哨兵檔存在、或 env TI_DISPATCH_AUTO 為真＝auto（PM 全權派工）。"""
    if os.getenv("TI_DISPATCH_AUTO", "0") not in ("0", "false", "False", ""):
        return True
    return DISPATCH_AUTO_FILE.exists()


def has_api_key() -> bool:
    """是否設定了 Anthropic 金鑰（端到端執行需要；單元測試不需要）。"""
    return bool(os.getenv("ANTHROPIC_API_KEY"))


def claude_cli_logged_in() -> bool:
    """是否已透過 claude CLI 登入（訂閱 OAuth 憑證），SDK 子程序會沿用。"""
    return CLAUDE_CREDENTIALS_FILE.exists()


def codex_cli_available() -> bool:
    """Codex CLI 是否可執行。TI_CODEX_BIN 可填絕對路徑或 PATH 內的命令名。"""
    return shutil.which(CODEX_BIN) is not None


def codex_cli_logged_in() -> bool:
    """Codex CLI 是否具備非互動執行憑證。

    codex exec 可用單次環境變數 CODEX_API_KEY，或沿用 CODEX_HOME/auth.json 的既有登入。
    """
    if CODEX_API_KEY:
        return True
    home = Path(CODEX_HOME).expanduser() if CODEX_HOME else Path.home() / ".codex"
    return (home / "auth.json").exists()


def antigravity_cli_available() -> bool:
    """Antigravity CLI 是否可執行。登入狀態由 `agy -p` 執行時回報並由 provider pause 收斂。"""
    return shutil.which(ANTIGRAVITY_BIN) is not None


def provider_ready() -> bool:
    """目前選定的 provider 是否具備可執行的憑證/設定。"""
    if PROVIDER == "codex":
        return codex_cli_available() and codex_cli_logged_in()
    if PROVIDER == "antigravity":
        return antigravity_cli_available()
    if PROVIDER == "minimax":
        # MiniMax base_url 有預設端點，故只認 API key 是否填妥。
        return bool(MINIMAX_API_KEY)
    if PROVIDER == "gemini":
        return bool(GEMINI_API_KEY)
    if PROVIDER == "openai":
        return bool(OPENAI_API_KEY or OPENAI_BASE_URL)
    # claude provider：環境變數金鑰，或已登入的 claude CLI 訂閱皆可。
    return has_api_key() or claude_cli_logged_in()


def reload() -> None:
    """重新從環境變數載入「可由 UI 調整」的設定，讓變更即時生效（無需重啟）。

    涵蓋 provider／模型／OpenAI／GitHub 發佈／並行／角色／輪數，以及「進階流程」開關
    （huddle／critic／notes／lessons／reflexion／客觀閘門／self-refine／rlimits）——這些
    設定面板可改的項目；其餘（門禁、路徑、伺服器位址）維持啟動時的值。
    """
    global PROVIDER, MODEL_LEAD, MODEL_FAST, ROLE_MODELS, ROLE_PROVIDERS
    global PM_PIN_PROVIDER, PM_PIN_MODEL
    global CLAUDE_SCOPED_FALLBACK_MODEL, CLAUDE_SCOPED_LIMIT_THRESHOLD
    global OPENAI_API_KEY, OPENAI_BASE_URL, OPENAI_MODEL_LEAD, OPENAI_MODEL_FAST, OPENAI_MAX_STEPS
    global MINIMAX_API_KEY, MINIMAX_BASE_URL, MINIMAX_MODEL_LEAD, MINIMAX_MODEL_FAST
    global GEMINI_API_KEY, GEMINI_BASE_URL, GEMINI_MODEL_LEAD, GEMINI_MODEL_FAST
    global CODEX_BIN, CODEX_HOME, CODEX_API_KEY, CODEX_MODEL_LEAD, CODEX_MODEL_FAST
    global CODEX_SANDBOX, CODEX_BYPASS_SANDBOX
    global ANTIGRAVITY_BIN, ANTIGRAVITY_MODEL_LEAD, ANTIGRAVITY_MODEL_FAST
    global ANTIGRAVITY_SANDBOX, ANTIGRAVITY_SKIP_PERMISSIONS
    global GITHUB_TOKEN, TI_GIT_CRED_LEGACY, PUBLISH_REPO, PUBLISH_BASE, PUBLISH_AUTO, PUBLISH_MERGE
    global PUBLISH_CI_TIMEOUT, PUBLISH_CI_INTERVAL, PUBLISH_MERGE_RETRIES
    global PUBLISH_CI_MAX_ROUNDS, PUBLISH_CI_GRACE, PUBLISH_OWNER_ALLOWLIST
    global MERGE_BEHIND_RETRIES
    global LEAD_ROLES, OPTIONAL_ROLES, MAX_TASKS, TASK_MAX_ROUNDS, DEBATE_ROUNDS
    global DISCUSS_MAX_ROUNDS, DISCUSS_MODE, AGENDA_ROUNDS
    global PARALLEL_TASKS_ENABLED, PARALLEL_LANES, LLM_MAX_CONCURRENCY
    global HUDDLE_ENABLED, CRITIC_ENABLED, CRITIC_MAX_REJECTS, NOTES_ENABLED, NOTES_MAX_CHARS
    global TASK_HELP_ENABLED, TASK_HELP_MAX
    global DYNAMIC_STEP_BUDGET, RECRUIT_MAX, DEFAULT_WORKFLOW
    global VOTE_ENABLED, VOTE_MAX
    global LESSONS_ENABLED
    global REFLEXION_ENABLED, OBJECTIVE_GATE, SELF_REFINE_ITERS, RLIMITS_ENABLED
    global TURN_IDLE_TIMEOUT, TURN_HARD_TIMEOUT, AUTOPILOT_STALL_TIMEOUT
    global EXPERT_RATE_LIMIT_RETRIES, EXPERT_RATE_LIMIT_BACKOFF, EXPERT_RATE_LIMIT_BACKOFF_CAP
    global EXPERT_RATE_LIMIT_BACKOFF_JITTER
    global KNOWLEDGE_ENABLED, KNOWLEDGE_MAX_CHARS, CLARIFY_ENABLED, CLARIFY_TIMEOUT
    global CLARIFY_MAX_QUESTIONS, DISCOVER_ROLES
    global BLUEPRINT_ENABLED, BLUEPRINT_SEED_MAX, ADR_ENABLED, ADR_MAX
    global RESEARCH_TOOLS_ENABLED, RESEARCH_ALLOWED_DOMAINS
    global RESEARCH_FETCH_TIMEOUT, RESEARCH_FETCH_MAX_CHARS
    global LESSONS_DISTILL, LESSONS_DISTILL_THRESHOLD, LESSONS_DISTILL_INTERVAL
    global APPRAISAL_ENABLED, APPRAISAL_MAX_STORE
    global ROLES_DIR, AUTOPILOT_NORTH_STAR
    global AUTOPILOT_QUOTA_GATE, AUTOPILOT_QUOTA_MAX_SLEEP, QUOTA_STALE_MAX
    global AUTOPILOT_DAILY_PR_BUDGET
    global AUTOPILOT_WORKFLOW_TRIAGE, AUTOPILOT_TRIAGE_TIMEOUT
    global AUTOPILOT_DEPLOY_CHECK_INTERVAL, AUTOPILOT_DEPLOY_FAIL_BACKOFF
    global AUTOPILOT_AUTO_MERGE, AUTOPILOT_MERGE_FAST_WAIT, AUTOPILOT_MERGE_MAX_AGE
    global LINT_AUTOFORMAT, AUTOPILOT_FOLLOWUP_VALUE_GATE
    global AUTOPILOT_PREFILTER_IMPLEMENTED, AUTOPILOT_PREFILTER_RATIO
    global AUTOPILOT_PREFILTER_LOOKBACK_DAYS
    global AUTOPILOT_INVESTIGATION_LANE, AUTOPILOT_INVESTIGATION_TIMEOUT
    global AUTOPILOT_INVESTIGATION_PARALLEL
    global AUTOPILOT_INVESTIGATION_REFUTE
    global EXPERT_LINT_HOOK, EXPERT_LINT_TIMEOUT

    global EXPERT_SKILLS, EXPERT_SKILLS_ROLES
    global CONVENTIONS_CARD
    global EXPERT_EFFORT, EXPERT_EFFORT_MAP
    global EXPERT_IDLE_STOP_S, EXPERT_IDLE_STOP_EXEMPT
    global AUTOPILOT_LOOP_STALL_S, AUTOPILOT_RECONCILE_INTERVAL_S, NOTIFY_WEBHOOK
    global AUTOPILOT_TIMEOUT_AUTOSPLIT, AUTOPILOT_SPLIT_MAX_DEPTH, AUTOPILOT_SPLIT_MAX_SUBTASKS
    global AUTOPILOT_FOLLOWUP_MAX_PER_TASK, AUTOPILOT_FOLLOWUP_MAX_GEN
    global AUTOPILOT_DISCOVERED_DAILY_CAP, AUTOPILOT_RETRY_COOLDOWN_S
    global CLAUDE_ROTATE, CLAUDE_ACCOUNT_PREFERRED, CLAUDE_ROTATE_THRESHOLD
    global CLAUDE_ROTATE_MARGIN, CLAUDE_ROTATE_RESET_EDGE, CLAUDE_ROTATE_RESET_EDGE_7D
    global CLAUDE_ROTATE_SCOPED
    PROVIDER = os.getenv("TI_PROVIDER", "claude").lower()
    AUTOPILOT_NORTH_STAR = os.getenv(
        "TI_AUTOPILOT_NORTH_STAR",
        "持續提升 Ti 程式品質；強化 agent 間與跨 provider 溝通協作效能",
    )
    ROLES_DIR = Path(os.getenv("TI_ROLES_DIR", str(PROJECT_ROOT / "roles")))
    PARALLEL_TASKS_ENABLED = os.getenv("TI_PARALLEL_TASKS", "1") not in ("0", "false", "False", "")
    PARALLEL_LANES = int(os.getenv("TI_PARALLEL_LANES", "3"))
    LLM_MAX_CONCURRENCY = int(os.getenv("TI_LLM_MAX_CONCURRENCY", "9"))
    LEAD_ROLES = {r.strip() for r in os.getenv("TI_LEAD_ROLES", "pm").split(",") if r.strip()}
    OPTIONAL_ROLES = {
        r.strip()
        for r in os.getenv("TI_OPTIONAL_ROLES", "researcher,architect,security,devops").split(",")
        if r.strip()
    }
    MAX_TASKS = int(os.getenv("TI_MAX_TASKS", "5"))
    TASK_MAX_ROUNDS = int(os.getenv("TI_MAX_ROUNDS", "3"))
    DEBATE_ROUNDS = int(os.getenv("TI_DEBATE_ROUNDS", "2"))
    DISCUSS_MAX_ROUNDS = _discuss_max_rounds()  # 依賴 DEBATE_ROUNDS，須在其後重算
    DISCUSS_MODE = _discuss_mode()
    AGENDA_ROUNDS = _agenda_rounds()
    MODEL_LEAD = os.getenv("TI_MODEL_LEAD", "claude-opus-4-8")
    MODEL_FAST = os.getenv("TI_MODEL_FAST", "claude-sonnet-4-6")
    ROLE_MODELS = _role_models()
    ROLE_PROVIDERS = _role_providers()
    PM_PIN_PROVIDER = os.getenv("TI_PM_PIN_PROVIDER", "claude").strip().lower()
    PM_PIN_MODEL = os.getenv("TI_PM_PIN_MODEL", "claude-fable-5").strip()
    CLAUDE_SCOPED_FALLBACK_MODEL = os.getenv(
        "TI_CLAUDE_SCOPED_FALLBACK_MODEL", "claude-opus-4-8"
    ).strip()
    CLAUDE_SCOPED_LIMIT_THRESHOLD = float(os.getenv("TI_CLAUDE_SCOPED_LIMIT_THRESHOLD", "95"))
    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
    OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "")
    OPENAI_MODEL_LEAD = os.getenv("TI_OPENAI_MODEL_LEAD", "gpt-4o")
    OPENAI_MODEL_FAST = os.getenv("TI_OPENAI_MODEL_FAST", "gpt-4o-mini")
    OPENAI_MAX_STEPS = int(os.getenv("TI_OPENAI_MAX_STEPS", "12"))
    MINIMAX_API_KEY = os.getenv("MINIMAX_API_KEY", "")
    MINIMAX_BASE_URL = os.getenv("MINIMAX_BASE_URL", "https://api.minimax.io/v1")
    MINIMAX_MODEL_LEAD = os.getenv("TI_MINIMAX_MODEL_LEAD", "MiniMax-M3")
    MINIMAX_MODEL_FAST = os.getenv("TI_MINIMAX_MODEL_FAST", "MiniMax-M3")
    GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
    GEMINI_BASE_URL = os.getenv(
        "GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai/"
    )
    GEMINI_MODEL_LEAD = os.getenv("TI_GEMINI_MODEL_LEAD", "gemini-2.5-pro")
    GEMINI_MODEL_FAST = os.getenv("TI_GEMINI_MODEL_FAST", "gemini-2.5-flash")
    CODEX_BIN = os.getenv("TI_CODEX_BIN", "codex")
    CODEX_HOME = os.getenv("CODEX_HOME", "")
    CODEX_API_KEY = os.getenv("CODEX_API_KEY", "")
    CODEX_MODEL_LEAD = os.getenv("TI_CODEX_MODEL_LEAD", "")
    CODEX_MODEL_FAST = os.getenv("TI_CODEX_MODEL_FAST", "")
    CODEX_SANDBOX = _codex_sandbox()
    CODEX_BYPASS_SANDBOX = os.getenv("TI_CODEX_BYPASS_SANDBOX", "0") not in (
        "0",
        "false",
        "False",
        "",
    )
    ANTIGRAVITY_BIN = os.getenv("TI_ANTIGRAVITY_BIN", "agy")
    ANTIGRAVITY_MODEL_LEAD = os.getenv("TI_ANTIGRAVITY_MODEL_LEAD", "")
    ANTIGRAVITY_MODEL_FAST = os.getenv("TI_ANTIGRAVITY_MODEL_FAST", "")
    ANTIGRAVITY_SANDBOX = os.getenv("TI_ANTIGRAVITY_SANDBOX", "1") not in (
        "0",
        "false",
        "False",
        "",
    )
    ANTIGRAVITY_SKIP_PERMISSIONS = os.getenv("TI_ANTIGRAVITY_SKIP_PERMISSIONS", "1") not in (
        "0",
        "false",
        "False",
        "",
    )
    GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
    # publisher push 路徑需 git ≥ 2.31；legacy/舊 git 時 push 403 屬 fail-closed 預期。
    TI_GIT_CRED_LEGACY = os.getenv("TI_GIT_CRED_LEGACY", "0") not in (
        "0",
        "false",
        "False",
        "",
    )
    PUBLISH_REPO = os.getenv("TI_PUBLISH_REPO", "")
    PUBLISH_BASE = os.getenv("TI_PUBLISH_BASE", "main")
    PUBLISH_OWNER_ALLOWLIST = _publish_owner_allowlist()
    PUBLISH_AUTO = os.getenv("TI_PUBLISH_AUTO", "0") not in ("0", "false", "False", "")
    PUBLISH_MERGE = os.getenv("TI_PUBLISH_MERGE", "0") not in ("0", "false", "False", "")
    PUBLISH_CI_TIMEOUT = int(os.getenv("TI_PUBLISH_CI_TIMEOUT", "600"))
    PUBLISH_CI_INTERVAL = int(os.getenv("TI_PUBLISH_CI_INTERVAL", "10"))
    PUBLISH_MERGE_RETRIES = int(os.getenv("TI_PUBLISH_MERGE_RETRIES", "3"))
    MERGE_BEHIND_RETRIES = int(os.getenv("TI_MERGE_BEHIND_RETRIES", "4"))
    PUBLISH_CI_MAX_ROUNDS = int(os.getenv("TI_PUBLISH_CI_MAX_ROUNDS", "5"))
    PUBLISH_CI_GRACE = int(os.getenv("TI_PUBLISH_CI_GRACE", "120"))
    # 進階流程開關（設定面板「進階」組）。消費端皆讀即時全域值，故 reload 後下次討論生效。
    # 預設值須與檔頂宣告一致（critic 為唯一預設關閉者，理由見檔頂註解）。
    HUDDLE_ENABLED = os.getenv("TI_HUDDLE", "1") not in ("0", "false", "False", "")
    TASK_HELP_ENABLED = os.getenv("TI_TASK_HELP", "1") not in ("0", "false", "False", "")
    TASK_HELP_MAX = _env_int("TI_TASK_HELP_MAX", 1)
    CRITIC_ENABLED = os.getenv("TI_CRITIC", "0") not in ("0", "false", "False", "")
    CRITIC_MAX_REJECTS = int(os.getenv("TI_CRITIC_MAX_REJECTS", "2"))
    DYNAMIC_STEP_BUDGET = _env_int("TI_DYNAMIC_STEP_BUDGET", 3)
    RECRUIT_MAX = _env_int("TI_RECRUIT_MAX", 3)
    VOTE_ENABLED = os.getenv("TI_VOTE_ENABLED", "1") not in ("0", "false", "False", "")
    VOTE_MAX = _env_int("TI_VOTE_MAX", 2)
    DEFAULT_WORKFLOW = os.getenv("TI_DEFAULT_WORKFLOW", "動態優先").strip()
    NOTES_ENABLED = os.getenv("TI_NOTES", "1") not in ("0", "false", "False", "")
    NOTES_MAX_CHARS = int(os.getenv("TI_NOTES_MAX_CHARS", "6000"))
    LESSONS_ENABLED = os.getenv("TI_LESSONS", "1") not in ("0", "false", "False", "")
    LESSONS_DISTILL = os.getenv("TI_LESSONS_DISTILL", "1") not in ("0", "false", "False", "")
    LESSONS_DISTILL_THRESHOLD = int(os.getenv("TI_LESSONS_DISTILL_THRESHOLD", "200"))
    LESSONS_DISTILL_INTERVAL = int(os.getenv("TI_LESSONS_DISTILL_INTERVAL", "86400"))
    APPRAISAL_ENABLED = os.getenv("TI_APPRAISAL", "1") not in ("0", "false", "False", "")
    APPRAISAL_MAX_STORE = int(os.getenv("TI_APPRAISAL_MAX_STORE", "2000"))
    REFLEXION_ENABLED = os.getenv("TI_REFLEXION", "1") not in ("0", "false", "False", "")
    OBJECTIVE_GATE = os.getenv("TI_OBJECTIVE_GATE", "1")
    SELF_REFINE_ITERS = int(os.getenv("TI_SELF_REFINE_ITERS", "1"))
    RLIMITS_ENABLED = os.getenv("TI_RLIMITS", "1") not in ("0", "false", "False", "")
    TURN_IDLE_TIMEOUT = _env_float("TI_TURN_IDLE_TIMEOUT", 240)
    TURN_HARD_TIMEOUT = _env_float("TI_TURN_TIMEOUT", 1800)
    # 與 TURN_* 同組重載，使停滯門檻執行期可調（須維持 > TURN_HARD_TIMEOUT 的不變量）。
    AUTOPILOT_STALL_TIMEOUT = int(os.getenv("TI_AUTOPILOT_STALL_TIMEOUT", "2400"))
    EXPERT_RATE_LIMIT_RETRIES = int(os.getenv("TI_RATELIMIT_RETRIES", "3"))
    EXPERT_RATE_LIMIT_BACKOFF = _env_float("TI_RATELIMIT_BACKOFF", 2.0)
    EXPERT_RATE_LIMIT_BACKOFF_CAP = _env_float("TI_RATELIMIT_BACKOFF_CAP", 60.0)
    EXPERT_RATE_LIMIT_BACKOFF_JITTER = _env_float("TI_RATELIMIT_BACKOFF_JITTER", 0.5)
    KNOWLEDGE_ENABLED = os.getenv("TI_KNOWLEDGE", "1") not in ("0", "false", "False", "")
    KNOWLEDGE_MAX_CHARS = int(os.getenv("TI_KNOWLEDGE_MAX_CHARS", "4000"))
    CLARIFY_ENABLED = os.getenv("TI_CLARIFY", "1") not in ("0", "false", "False", "")
    CLARIFY_TIMEOUT = _env_float("TI_CLARIFY_TIMEOUT", 180.0)
    CLARIFY_MAX_QUESTIONS = int(os.getenv("TI_CLARIFY_MAX_QUESTIONS", "4"))
    DISCOVER_ROLES = [
        r.strip()
        for r in os.getenv("TI_DISCOVER_ROLES", "senior,pm,researcher").split(",")
        if r.strip()
    ]
    BLUEPRINT_ENABLED = os.getenv("TI_BLUEPRINT", "0") not in ("0", "false", "False", "")
    BLUEPRINT_SEED_MAX = int(os.getenv("TI_BLUEPRINT_SEED_MAX", "5"))
    ADR_ENABLED = os.getenv("TI_ADR", "0") not in ("0", "false", "False", "")
    ADR_MAX = int(os.getenv("TI_ADR_MAX", "8"))
    RESEARCH_TOOLS_ENABLED = os.getenv("TI_RESEARCH_TOOLS", "0") not in ("0", "false", "False", "")
    RESEARCH_ALLOWED_DOMAINS = [
        d.strip().lower()
        for d in os.getenv("TI_RESEARCH_ALLOWED_DOMAINS", "").split(",")
        if d.strip()
    ]
    RESEARCH_FETCH_TIMEOUT = float(os.getenv("TI_RESEARCH_FETCH_TIMEOUT", "20"))
    RESEARCH_FETCH_MAX_CHARS = int(os.getenv("TI_RESEARCH_FETCH_MAX_CHARS", "8000"))
    # autopilot 額度感知節奏（預設值須與檔頂宣告一致）
    AUTOPILOT_QUOTA_GATE = os.getenv("TI_AUTOPILOT_QUOTA_GATE", "1") not in (
        "0",
        "false",
        "False",
        "",
    )
    AUTOPILOT_QUOTA_MAX_SLEEP = int(os.getenv("TI_AUTOPILOT_QUOTA_MAX_SLEEP", "1800"))
    # 每日 PR 成本熔斷（預設值須與檔頂宣告一致）
    AUTOPILOT_DAILY_PR_BUDGET = int(os.getenv("TI_AUTOPILOT_DAILY_PR_BUDGET", "0"))
    # PM workflow 分診（預設值須與檔頂宣告一致）
    AUTOPILOT_WORKFLOW_TRIAGE = os.getenv("TI_AUTOPILOT_WORKFLOW_TRIAGE", "0") not in (
        "0",
        "false",
        "False",
        "",
    )
    AUTOPILOT_TRIAGE_TIMEOUT = int(os.getenv("TI_AUTOPILOT_TRIAGE_TIMEOUT", "60"))
    AUTOPILOT_DEPLOY_CHECK_INTERVAL = int(os.getenv("TI_AUTOPILOT_DEPLOY_CHECK_INTERVAL", "300"))
    AUTOPILOT_DEPLOY_FAIL_BACKOFF = int(os.getenv("TI_AUTOPILOT_DEPLOY_FAIL_BACKOFF", "1800"))
    AUTOPILOT_AUTO_MERGE = os.getenv("TI_AUTOPILOT_AUTO_MERGE", "1") not in (
        "0",
        "false",
        "False",
        "",
    )
    AUTOPILOT_MERGE_FAST_WAIT = int(os.getenv("TI_AUTOPILOT_MERGE_FAST_WAIT", "180"))
    AUTOPILOT_MERGE_MAX_AGE = int(os.getenv("TI_AUTOPILOT_MERGE_MAX_AGE", "7200"))
    # lint 閘門自動格式化（預設值須與檔頂宣告一致）
    LINT_AUTOFORMAT = os.getenv("TI_LINT_AUTOFORMAT", "1") not in ("0", "false", "False", "")
    AUTOPILOT_PREFILTER_IMPLEMENTED = os.getenv("TI_AUTOPILOT_PREFILTER_IMPLEMENTED", "1") not in (
        "0",
        "false",
        "False",
        "",
    )
    AUTOPILOT_PREFILTER_RATIO = _env_float("TI_AUTOPILOT_PREFILTER_RATIO", 0.80)
    AUTOPILOT_PREFILTER_LOOKBACK_DAYS = _env_int("TI_AUTOPILOT_PREFILTER_LOOKBACK_DAYS", 60)
    AUTOPILOT_FOLLOWUP_VALUE_GATE = os.getenv("TI_AUTOPILOT_FOLLOWUP_VALUE_GATE", "1") not in (
        "0",
        "false",
        "False",
        "",
    )
    AUTOPILOT_INVESTIGATION_LANE = os.getenv("TI_AUTOPILOT_INVESTIGATION_LANE", "1") not in (
        "0",
        "false",
        "False",
        "",
    )
    AUTOPILOT_INVESTIGATION_TIMEOUT = int(os.getenv("TI_AUTOPILOT_INVESTIGATION_TIMEOUT", "1200"))
    AUTOPILOT_INVESTIGATION_PARALLEL = os.getenv(
        "TI_AUTOPILOT_INVESTIGATION_PARALLEL", "0"
    ) not in ("0", "false", "False", "")
    AUTOPILOT_INVESTIGATION_REFUTE = os.getenv("TI_AUTOPILOT_INVESTIGATION_REFUTE", "1") not in (
        "0",
        "false",
        "False",
        "",
    )
    EXPERT_LINT_HOOK = os.getenv("TI_EXPERT_LINT_HOOK", "1") not in ("0", "false", "False", "")
    EXPERT_LINT_TIMEOUT = float(os.getenv("TI_EXPERT_LINT_TIMEOUT", "15"))

    EXPERT_SKILLS = os.getenv("TI_EXPERT_SKILLS", "0") not in ("0", "false", "False", "")
    EXPERT_SKILLS_ROLES = frozenset(
        r.strip()
        for r in os.getenv("TI_EXPERT_SKILLS_ROLES", "engineer,senior,qa").split(",")
        if r.strip()
    )
    CONVENTIONS_CARD = os.getenv("TI_CONVENTIONS_CARD", "1") not in ("0", "false", "False", "")
    EXPERT_EFFORT = os.getenv("TI_EXPERT_EFFORT", "").strip().lower()
    EXPERT_EFFORT_MAP = _parse_effort_map(os.getenv("TI_EXPERT_EFFORT_MAP", ""))
    AUTOPILOT_LOOP_STALL_S = int(os.getenv("TI_AUTOPILOT_LOOP_STALL_S", "900"))
    AUTOPILOT_RECONCILE_INTERVAL_S = int(os.getenv("TI_AUTOPILOT_RECONCILE_INTERVAL_S", "300"))
    NOTIFY_WEBHOOK = os.getenv("TI_NOTIFY_WEBHOOK", "").strip()
    EXPERT_IDLE_STOP_S = int(os.getenv("TI_EXPERT_IDLE_STOP_S", "0"))
    EXPERT_IDLE_STOP_EXEMPT = frozenset(
        r.strip().lower()
        for r in os.getenv("TI_EXPERT_IDLE_STOP_EXEMPT", "pm").split(",")
        if r.strip()
    )
    AUTOPILOT_TIMEOUT_AUTOSPLIT = os.getenv("TI_AUTOPILOT_TIMEOUT_AUTOSPLIT", "1") not in (
        "0",
        "false",
        "False",
        "",
    )
    AUTOPILOT_SPLIT_MAX_DEPTH = int(os.getenv("TI_AUTOPILOT_SPLIT_MAX_DEPTH", "2"))
    AUTOPILOT_SPLIT_MAX_SUBTASKS = int(os.getenv("TI_AUTOPILOT_SPLIT_MAX_SUBTASKS", "4"))
    AUTOPILOT_FOLLOWUP_MAX_PER_TASK = int(os.getenv("TI_AUTOPILOT_FOLLOWUP_MAX_PER_TASK", "3"))
    AUTOPILOT_FOLLOWUP_MAX_GEN = int(os.getenv("TI_AUTOPILOT_FOLLOWUP_MAX_GEN", "3"))
    AUTOPILOT_DISCOVERED_DAILY_CAP = int(os.getenv("TI_AUTOPILOT_DISCOVERED_DAILY_CAP", "20"))
    AUTOPILOT_RETRY_COOLDOWN_S = int(os.getenv("TI_AUTOPILOT_RETRY_COOLDOWN_S", "600"))
    # provider 額度快照 SWR（預設值須與檔頂宣告一致）
    QUOTA_STALE_MAX = _env_float("TI_QUOTA_STALE_MAX", 300.0)
    # Claude 訂閱雙帳號自動輪替（預設值須與檔頂宣告一致）
    CLAUDE_ROTATE = os.getenv("TI_CLAUDE_ROTATE", "1") not in ("0", "false", "False", "")
    CLAUDE_ACCOUNT_PREFERRED = os.getenv("TI_CLAUDE_ACCOUNT_PREFERRED", "B")
    CLAUDE_ROTATE_THRESHOLD = _env_float("TI_CLAUDE_ROTATE_THRESHOLD", 95.0)
    CLAUDE_ROTATE_MARGIN = _env_float("TI_CLAUDE_ROTATE_MARGIN", 10.0)
    CLAUDE_ROTATE_RESET_EDGE = _env_float("TI_CLAUDE_ROTATE_RESET_EDGE", 900.0)
    CLAUDE_ROTATE_RESET_EDGE_7D = _env_float("TI_CLAUDE_ROTATE_RESET_EDGE_7D", 21600.0)
    CLAUDE_ROTATE_SCOPED = os.getenv("TI_CLAUDE_ROTATE_SCOPED", "1") not in (
        "0",
        "false",
        "False",
        "",
    )
