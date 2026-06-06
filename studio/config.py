"""集中設定。模型 ID、討論輪數、伺服器與 workspace 路徑都放這裡，方便日後調整。"""

from __future__ import annotations

import os
import secrets
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

# 單一專家發言（含工具操作）的回合上限，避免 agent 卡住。
MAX_TURNS_PER_TURN = int(os.getenv("TI_MAX_TURNS", "40"))

# --- 確定性執行（runner）-----------------------------------------------
# 自測 / Demo 的執行逾時（秒）與輸出字數上限。
DEMO_TIMEOUT = int(os.getenv("TI_DEMO_TIMEOUT", "60"))
DEMO_MAX_OUTPUT = int(os.getenv("TI_DEMO_MAX_OUTPUT", "8000"))

# 是否在 workspace 內建立獨立 git repo 並做階段性 commit。
ENABLE_GIT = os.getenv("TI_ENABLE_GIT", "1") not in ("0", "false", "False", "")

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
