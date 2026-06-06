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
# 實作→驗證→審查 的最大改進輪數，避免無止盡迴圈。
MAX_ROUNDS = int(os.getenv("TI_MAX_ROUNDS", "3"))

# 單一專家發言（含工具操作）的回合上限，避免 agent 卡住。
MAX_TURNS_PER_TURN = int(os.getenv("TI_MAX_TURNS", "40"))

# --- 路徑 ---------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
WORKSPACE_ROOT = Path(os.getenv("TI_WORKSPACE_ROOT", str(PROJECT_ROOT / "workspaces")))
WEB_DIR = PROJECT_ROOT / "web"

# --- 伺服器 -------------------------------------------------------------
HOST = os.getenv("TI_HOST", "0.0.0.0")
PORT = int(os.getenv("TI_PORT", "8000"))


def has_api_key() -> bool:
    """是否設定了 Anthropic 金鑰（端到端執行需要；單元測試不需要）。"""
    return bool(os.getenv("ANTHROPIC_API_KEY"))
