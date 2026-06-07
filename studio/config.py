"""集中設定。模型 ID、討論輪數、伺服器與 workspace 路徑都放這裡，方便日後調整。"""

from __future__ import annotations

import os
import secrets
import shutil
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# --- Provider / 模型 ----------------------------------------------------
# 後端 LLM provider：claude（預設，走 Agent SDK 自帶工具）或 openai（含 OpenAI 相容/本地模型）。
PROVIDER = os.getenv("TI_PROVIDER", "claude").lower()

# Claude 模型 ID。PM / 高級工程師需要較強的推理；工程師 / 驗證工程師偏重速度。
MODEL_LEAD = os.getenv("TI_MODEL_LEAD", "claude-opus-4-8")
MODEL_FAST = os.getenv("TI_MODEL_FAST", "claude-sonnet-4-6")

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

# --- 內部討論機制（卡關 huddle，預設關閉以保既有測試/行為向後相容）---------
# 開啟後：任務跑滿 TASK_MAX_ROUNDS 仍未通過時，召集團隊 huddle 找替代方案並給 1 輪重試，
# 仍失敗則明確標記為「已知限制」而非靜默帶過。離線 demo 由腳本自行開啟此開關展示。
HUDDLE_ENABLED = os.getenv("TI_HUDDLE", "0") not in ("0", "false", "False", "")

# 異議檢查（critic）：放行前由獨立 critic 專挑「為何還不算完成」，提出實質反對才退回。
# 採「換人」原則保獨立性（任務審查用 pm 視角、最終驗收用 senior 視角），預設關閉。
CRITIC_ENABLED = os.getenv("TI_CRITIC", "0") not in ("0", "false", "False", "")

# 共用知識庫（workspace 內 NOTES.md）：跨任務累積踩過的坑/決策/後續，實作時讀回、結束時寫入。
# 不進交付物與檔案清單（見 workspace._IGNORE）。預設關閉以保既有行為。
NOTES_ENABLED = os.getenv("TI_NOTES", "0") not in ("0", "false", "False", "")

# 停滯守門：改進迴圈連續 STALL_ROUNDS 輪只重述（文字高度相似且無檔案變動）就提早收斂，
# 避免燒 token。<=1 視為停用。預設值刻意大於離線示範每任務實際圈數，使既有流程不誤觸；
# 且 _stalled 在無 cwd 或關閉 git 時一律不偵測（保護 cwd=None 的單元測試）。
STALL_ROUNDS = int(os.getenv("TI_STALL_ROUNDS", "3"))

# 單一專家發言（含工具操作）的回合上限，避免 agent 卡住。
MAX_TURNS_PER_TURN = int(os.getenv("TI_MAX_TURNS", "40"))

# 啟用哪些「可選角色」（核心 4 角色永遠在）。逗號分隔；清空則只剩核心 4 角色。
# 多一個角色 = 每場討論多幾次 LLM 呼叫（更耗額度、更久），要省可逐一移除。
OPTIONAL_ROLES = {
    r.strip()
    for r in os.getenv(
        "TI_OPTIONAL_ROLES", "researcher,architect,security,devops"
    ).split(",")
    if r.strip()
}

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


# --- 路徑 ---------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
WORKSPACE_ROOT = Path(os.getenv("TI_WORKSPACE_ROOT", str(PROJECT_ROOT / "workspaces")))
HISTORY_ROOT = Path(os.getenv("TI_HISTORY_ROOT", str(PROJECT_ROOT / "history")))
WEB_DIR = PROJECT_ROOT / "web"

# --- 伺服器 -------------------------------------------------------------
HOST = os.getenv("TI_HOST", "0.0.0.0")
PORT = int(os.getenv("TI_PORT", "8000"))

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
AUTOPILOT_PAUSE_FILE = Path(os.getenv("TI_AUTOPILOT_PAUSE_FILE", str(PROJECT_ROOT / "AUTOPILOT_PAUSED")))
AUTOPILOT_DRYRUN = os.getenv("TI_AUTOPILOT_DRYRUN", "0") not in ("0", "false", "False", "")


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

    僅涵蓋 provider / 模型 / OpenAI / GitHub 發佈這幾組可在設定頁修改的項目；
    其餘（門禁、流程輪數、路徑、伺服器位址）維持啟動時的值。
    """
    global PROVIDER, MODEL_LEAD, MODEL_FAST
    global OPENAI_API_KEY, OPENAI_BASE_URL, OPENAI_MODEL_LEAD, OPENAI_MODEL_FAST, OPENAI_MAX_STEPS
    global GITHUB_TOKEN, PUBLISH_REPO, PUBLISH_BASE, PUBLISH_AUTO
    PROVIDER = os.getenv("TI_PROVIDER", "claude").lower()
    MODEL_LEAD = os.getenv("TI_MODEL_LEAD", "claude-opus-4-8")
    MODEL_FAST = os.getenv("TI_MODEL_FAST", "claude-sonnet-4-6")
    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
    OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "")
    OPENAI_MODEL_LEAD = os.getenv("TI_OPENAI_MODEL_LEAD", "gpt-4o")
    OPENAI_MODEL_FAST = os.getenv("TI_OPENAI_MODEL_FAST", "gpt-4o-mini")
    OPENAI_MAX_STEPS = int(os.getenv("TI_OPENAI_MAX_STEPS", "12"))
    GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
    PUBLISH_REPO = os.getenv("TI_PUBLISH_REPO", "")
    PUBLISH_BASE = os.getenv("TI_PUBLISH_BASE", "main")
    PUBLISH_AUTO = os.getenv("TI_PUBLISH_AUTO", "0") not in ("0", "false", "False", "")
