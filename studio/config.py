"""集中設定。模型 ID、討論輪數、伺服器與 workspace 路徑都放這裡，方便日後調整。"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# --- 模型 ---------------------------------------------------------------
# 把模型 ID 集中於此，方便日後更新。PM / 高級工程師需要較強的推理；
# 工程師 / 驗證工程師偏重速度與大量工具操作。
MODEL_LEAD = os.getenv("TI_MODEL_LEAD", "claude-opus-4-8")
MODEL_FAST = os.getenv("TI_MODEL_FAST", "claude-sonnet-4-6")

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

# --- 發佈到 GitHub（對外、預設關閉）------------------------------------
# 需同時設定 GITHUB_TOKEN 與 TI_PUBLISH_REPO（owner/repo）才會啟用。
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
PUBLISH_REPO = os.getenv("TI_PUBLISH_REPO", "")          # 例：octocat/outputs
PUBLISH_BASE = os.getenv("TI_PUBLISH_BASE", "main")      # PR 目標分支
# 專案完成後是否自動發佈（預設關閉，避免非預期的對外推送）。
PUBLISH_AUTO = os.getenv("TI_PUBLISH_AUTO", "0") not in ("0", "false", "False", "")

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
