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

load_dotenv()


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


# --- Provider / 模型 ----------------------------------------------------
# 後端 LLM provider：claude（預設，走 Agent SDK 自帶工具）或 openai（含 OpenAI 相容/本地模型）。
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

# OpenAI（相容）設定。OPENAI_BASE_URL 可指向本地模型（Ollama / LM Studio 等）。
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "")
OPENAI_MODEL_LEAD = os.getenv("TI_OPENAI_MODEL_LEAD", "gpt-4o")
OPENAI_MODEL_FAST = os.getenv("TI_OPENAI_MODEL_FAST", "gpt-4o-mini")
OPENAI_MAX_STEPS = int(os.getenv("TI_OPENAI_MAX_STEPS", "12"))

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
        logger.warning("環境變數 TI_DISCUSS_MAX_ROUNDS=%r 非整數，改用 DEBATE_ROUNDS=%s", raw, DEBATE_ROUNDS)
        return DEBATE_ROUNDS
    if val < 1:
        logger.warning("環境變數 TI_DISCUSS_MAX_ROUNDS=%s 須 ≥1，改用 DEBATE_ROUNDS=%s", val, DEBATE_ROUNDS)
        return DEBATE_ROUNDS
    return val


# 多角色討論（DiscussionEngine）的最大輪數上限；預設取 DEBATE_ROUNDS。
DISCUSS_MAX_ROUNDS = _discuss_max_rounds()

# 多角色討論模式白名單：legacy＝舊「工程師⇄高級工程師」兩人往返（預設，行為與現狀一致）；
# round_robin＝DiscussionEngine 依序發言；parallel＝同輪並行、輪間同步。
DISCUSS_MODES = ("legacy", "round_robin", "parallel")


def _discuss_mode() -> str:
    """TI_DISCUSS_MODE：白名單外（含拼錯）一律 fallback legacy（向後相容、絕不誤開新路徑）。"""
    raw = (os.getenv("TI_DISCUSS_MODE") or "").strip() or "legacy"
    if raw not in DISCUSS_MODES:
        logger.warning("環境變數 TI_DISCUSS_MODE=%r 不在白名單 %s，改用 legacy", raw, DISCUSS_MODES)
        return "legacy"
    return raw


DISCUSS_MODE = _discuss_mode()

# --- 內部討論機制（卡關 huddle）--------------------------------------------
# 開啟後：任務跑滿 TASK_MAX_ROUNDS 仍未通過時，召集團隊 huddle 找替代方案並給 1 輪重試，
# 仍失敗則明確標記為「已知限制」而非靜默帶過。只在「跑滿輪數仍失敗」的低頻路徑加成本，
# 換得失敗被明示——預設開啟（要省可關）。
HUDDLE_ENABLED = os.getenv("TI_HUDDLE", "1") not in ("0", "false", "False", "")

# 異議檢查（critic）：放行前由獨立 critic 專挑「為何還不算完成」，提出實質反對才退回。
# 採「換人」原則保獨立性（任務審查用 pm 視角、最終驗收用 senior 視角）。
# 唯一在「成功路徑」上加成本的學習開關（每個通過任務都多一次獨立呼叫）且有誤退回風險，
# 維持預設關閉（opt-in）。
CRITIC_ENABLED = os.getenv("TI_CRITIC", "0") not in ("0", "false", "False", "")

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

# 需求澄清階段：拆解前 PM 先就模糊需求向使用者反問關鍵問題（附預設假設），等回覆逾時則按
# 假設續行——流程絕不因等人而卡死。僅互動 session 生效（須有插話佇列）；autopilot／持續改良
# 迴圈等自主流程一律跳過。預設開啟：這是「說一句產品就能開工」的核心，無插話佇列時
# 自動跳過、天然向後相容。結論固化 workspace 的 PRD.md，抽出的「願景:」回填專案 meta。
CLARIFY_ENABLED = os.getenv("TI_CLARIFY", "1") not in ("0", "false", "False", "")
CLARIFY_TIMEOUT = float(os.getenv("TI_CLARIFY_TIMEOUT", "180"))  # 等使用者回覆的秒數
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
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
PUBLISH_REPO = os.getenv("TI_PUBLISH_REPO", "")  # 例：octocat/outputs
PUBLISH_BASE = os.getenv("TI_PUBLISH_BASE", "main")  # PR 目標分支
# 專案完成後是否自動發佈（預設關閉，避免非預期的對外推送）。
PUBLISH_AUTO = os.getenv("TI_PUBLISH_AUTO", "0") not in ("0", "false", "False", "")
# push 並開 PR 後是否自動合併進 base 分支（預設關閉，向後相容；開啟才形成自我改進閉環）。
PUBLISH_MERGE = os.getenv("TI_PUBLISH_MERGE", "0") not in ("0", "false", "False", "")
# 自動合併前等待 CI 的最長秒數、輪詢間隔、以及對 stale／409 的重試次數。
PUBLISH_CI_TIMEOUT = int(os.getenv("TI_PUBLISH_CI_TIMEOUT", "600"))
PUBLISH_CI_INTERVAL = int(os.getenv("TI_PUBLISH_CI_INTERVAL", "10"))
PUBLISH_MERGE_RETRIES = int(os.getenv("TI_PUBLISH_MERGE_RETRIES", "3"))
# 發佈後 CI 失敗時，讓團隊修正重推、再驗合併的最多輪數；以及每輪等新 commit 的 check
# 註冊出現的寬限秒數（避免「尚未註冊」被誤判為無 CI 而提前合併）。
PUBLISH_CI_MAX_ROUNDS = int(os.getenv("TI_PUBLISH_CI_MAX_ROUNDS", "5"))
PUBLISH_CI_GRACE = int(os.getenv("TI_PUBLISH_CI_GRACE", "120"))

# --- 登入 / 門禁（單一共用密碼，預設關閉）------------------------------
# 設定 TI_ACCESS_PASSWORD 後即啟用門禁：使用者需在登入頁輸入正確密碼才能進入工作室。
# 留空（預設）則完全停用認證，本地開發與離線示範不受影響、向後相容。
ACCESS_PASSWORD = os.getenv("TI_ACCESS_PASSWORD", "")
# 簽發 session cookie 的密鑰。留空時於程序啟動產生一組記憶體內隨機值（重啟即失效所有登入）。
AUTH_SECRET = os.getenv("TI_AUTH_SECRET", "") or secrets.token_hex(32)
# 登入 cookie 名稱與有效秒數（預設 7 天）。
AUTH_COOKIE = "ti_session"
AUTH_TTL = int(os.getenv("TI_AUTH_TTL", "604800"))


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
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def env_path() -> str:
    """持久化設定 / 秘密的 .env 路徑（settings 與 auth 共用此單一來源）。"""
    return str(PROJECT_ROOT / ".env")


WORKSPACE_ROOT = Path(os.getenv("TI_WORKSPACE_ROOT", str(PROJECT_ROOT / "workspaces")))
HISTORY_ROOT = Path(os.getenv("TI_HISTORY_ROOT", str(PROJECT_ROOT / "history")))
# 跨場次教訓庫持久化檔（見 LESSONS_ENABLED）。預設置於專案根，已列入 .gitignore，不進版控。
LESSONS_FILE = Path(os.getenv("TI_LESSONS_FILE", str(PROJECT_ROOT / "lessons.json")))
WEB_DIR = PROJECT_ROOT / "web"

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
AUTOPILOT_BRANCH = os.getenv("TI_AUTOPILOT_BRANCH", "main")  # 部署分支
AUTOPILOT_SERVICE = os.getenv("TI_AUTOPILOT_SERVICE", "ti.service")  # 重佈時要 restart 的服務
AUTOPILOT_HEALTH_URL = os.getenv("TI_AUTOPILOT_HEALTH_URL", "http://127.0.0.1:8021/api/health")
AUTOPILOT_COOLDOWN = int(os.getenv("TI_AUTOPILOT_COOLDOWN", "30"))  # 任務間最小喘息（秒）
# 部署 idle 守衛的 stale 門檻（秒）：status 卡在 running 但最後活動超過此值的討論視為死掉、
# 不再算「進行中」，避免崩潰沒收尾的 session 永久擋住 autodeploy / autopilot 重佈。預設 30 分。
DEPLOY_STALE_AFTER = int(os.getenv("TI_DEPLOY_STALE_AFTER", "1800"))
AUTOPILOT_PAUSE_FILE = Path(
    os.getenv("TI_AUTOPILOT_PAUSE_FILE", str(PROJECT_ROOT / "AUTOPILOT_PAUSED"))
)
AUTOPILOT_DRYRUN = os.getenv("TI_AUTOPILOT_DRYRUN", "0") not in ("0", "false", "False", "")
# 推送/合併安全旗標（預設皆取安全側）：
# AUTOPILOT_FORCE_PUSH：預設非強制推送；遠端已存在同名分支會中止。設 1 才略過中止並改用
#   `git push --force-with-lease --force-if-includes`（覆寫殘留分支用，絕不用裸 -f）。
AUTOPILOT_FORCE_PUSH = os.getenv("TI_AUTOPILOT_FORCE_PUSH", "0") not in ("0", "false", "False", "")
# AUTOPILOT_MERGE_ADMIN：預設不帶 `gh pr merge --admin`，讓 GitHub 分支保護生效。若目標 branch
#   有保護規則、需維持自動合併，須設 TI_AUTOPILOT_MERGE_ADMIN=1。
AUTOPILOT_MERGE_ADMIN = os.getenv("TI_AUTOPILOT_MERGE_ADMIN", "0") not in (
    "0",
    "false",
    "False",
    "",
)
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
# AUTOPILOT_EVAL_MEMORY：自我評估時回饋「近期成敗」給資深專家的筆數（每類 done/failed 各取
#   最近 N 筆）。讓評估記取自身成績單——避免重提已完成、避開已知失敗做法，越跑越聚焦。
#   0 = 停用（還原成無狀態評估）。
AUTOPILOT_EVAL_MEMORY = int(os.getenv("TI_AUTOPILOT_EVAL_MEMORY", "20"))


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


def has_api_key() -> bool:
    """是否設定了 Anthropic 金鑰（端到端執行需要；單元測試不需要）。"""
    return bool(os.getenv("ANTHROPIC_API_KEY"))


def claude_cli_logged_in() -> bool:
    """是否已透過 claude CLI 登入（訂閱 OAuth 憑證），SDK 子程序會沿用。"""
    return (Path.home() / ".claude" / ".credentials.json").exists()


def provider_ready() -> bool:
    """目前選定的 provider 是否具備可執行的憑證/設定。"""
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
    global PROVIDER, MODEL_LEAD, MODEL_FAST, ROLE_MODELS
    global OPENAI_API_KEY, OPENAI_BASE_URL, OPENAI_MODEL_LEAD, OPENAI_MODEL_FAST, OPENAI_MAX_STEPS
    global GITHUB_TOKEN, PUBLISH_REPO, PUBLISH_BASE, PUBLISH_AUTO, PUBLISH_MERGE
    global PUBLISH_CI_TIMEOUT, PUBLISH_CI_INTERVAL, PUBLISH_MERGE_RETRIES
    global PUBLISH_CI_MAX_ROUNDS, PUBLISH_CI_GRACE
    global LEAD_ROLES, OPTIONAL_ROLES, MAX_TASKS, TASK_MAX_ROUNDS, DEBATE_ROUNDS
    global DISCUSS_MAX_ROUNDS, DISCUSS_MODE
    global PARALLEL_TASKS_ENABLED, PARALLEL_LANES, LLM_MAX_CONCURRENCY
    global HUDDLE_ENABLED, CRITIC_ENABLED, NOTES_ENABLED, NOTES_MAX_CHARS, LESSONS_ENABLED
    global REFLEXION_ENABLED, OBJECTIVE_GATE, SELF_REFINE_ITERS, RLIMITS_ENABLED
    global TURN_IDLE_TIMEOUT, TURN_HARD_TIMEOUT
    global KNOWLEDGE_ENABLED, KNOWLEDGE_MAX_CHARS, CLARIFY_ENABLED, CLARIFY_TIMEOUT
    global CLARIFY_MAX_QUESTIONS, DISCOVER_ROLES
    global BLUEPRINT_ENABLED, BLUEPRINT_SEED_MAX, ADR_ENABLED, ADR_MAX
    global RESEARCH_TOOLS_ENABLED, RESEARCH_ALLOWED_DOMAINS
    global RESEARCH_FETCH_TIMEOUT, RESEARCH_FETCH_MAX_CHARS
    global LESSONS_DISTILL, LESSONS_DISTILL_THRESHOLD, LESSONS_DISTILL_INTERVAL
    PROVIDER = os.getenv("TI_PROVIDER", "claude").lower()
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
    MODEL_LEAD = os.getenv("TI_MODEL_LEAD", "claude-opus-4-8")
    MODEL_FAST = os.getenv("TI_MODEL_FAST", "claude-sonnet-4-6")
    ROLE_MODELS = _role_models()
    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
    OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "")
    OPENAI_MODEL_LEAD = os.getenv("TI_OPENAI_MODEL_LEAD", "gpt-4o")
    OPENAI_MODEL_FAST = os.getenv("TI_OPENAI_MODEL_FAST", "gpt-4o-mini")
    OPENAI_MAX_STEPS = int(os.getenv("TI_OPENAI_MAX_STEPS", "12"))
    GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
    PUBLISH_REPO = os.getenv("TI_PUBLISH_REPO", "")
    PUBLISH_BASE = os.getenv("TI_PUBLISH_BASE", "main")
    PUBLISH_AUTO = os.getenv("TI_PUBLISH_AUTO", "0") not in ("0", "false", "False", "")
    PUBLISH_MERGE = os.getenv("TI_PUBLISH_MERGE", "0") not in ("0", "false", "False", "")
    PUBLISH_CI_TIMEOUT = int(os.getenv("TI_PUBLISH_CI_TIMEOUT", "600"))
    PUBLISH_CI_INTERVAL = int(os.getenv("TI_PUBLISH_CI_INTERVAL", "10"))
    PUBLISH_MERGE_RETRIES = int(os.getenv("TI_PUBLISH_MERGE_RETRIES", "3"))
    PUBLISH_CI_MAX_ROUNDS = int(os.getenv("TI_PUBLISH_CI_MAX_ROUNDS", "5"))
    PUBLISH_CI_GRACE = int(os.getenv("TI_PUBLISH_CI_GRACE", "120"))
    # 進階流程開關（設定面板「進階」組）。消費端皆讀即時全域值，故 reload 後下次討論生效。
    # 預設值須與檔頂宣告一致（critic 為唯一預設關閉者，理由見檔頂註解）。
    HUDDLE_ENABLED = os.getenv("TI_HUDDLE", "1") not in ("0", "false", "False", "")
    CRITIC_ENABLED = os.getenv("TI_CRITIC", "0") not in ("0", "false", "False", "")
    NOTES_ENABLED = os.getenv("TI_NOTES", "1") not in ("0", "false", "False", "")
    NOTES_MAX_CHARS = int(os.getenv("TI_NOTES_MAX_CHARS", "6000"))
    LESSONS_ENABLED = os.getenv("TI_LESSONS", "1") not in ("0", "false", "False", "")
    LESSONS_DISTILL = os.getenv("TI_LESSONS_DISTILL", "1") not in ("0", "false", "False", "")
    LESSONS_DISTILL_THRESHOLD = int(os.getenv("TI_LESSONS_DISTILL_THRESHOLD", "200"))
    LESSONS_DISTILL_INTERVAL = int(os.getenv("TI_LESSONS_DISTILL_INTERVAL", "86400"))
    REFLEXION_ENABLED = os.getenv("TI_REFLEXION", "1") not in ("0", "false", "False", "")
    OBJECTIVE_GATE = os.getenv("TI_OBJECTIVE_GATE", "1")
    SELF_REFINE_ITERS = int(os.getenv("TI_SELF_REFINE_ITERS", "1"))
    RLIMITS_ENABLED = os.getenv("TI_RLIMITS", "1") not in ("0", "false", "False", "")
    TURN_IDLE_TIMEOUT = _env_float("TI_TURN_IDLE_TIMEOUT", 240)
    TURN_HARD_TIMEOUT = _env_float("TI_TURN_TIMEOUT", 1800)
    KNOWLEDGE_ENABLED = os.getenv("TI_KNOWLEDGE", "1") not in ("0", "false", "False", "")
    KNOWLEDGE_MAX_CHARS = int(os.getenv("TI_KNOWLEDGE_MAX_CHARS", "4000"))
    CLARIFY_ENABLED = os.getenv("TI_CLARIFY", "1") not in ("0", "false", "False", "")
    CLARIFY_TIMEOUT = float(os.getenv("TI_CLARIFY_TIMEOUT", "180"))
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
