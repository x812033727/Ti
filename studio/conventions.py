"""專家慣例卡：把「執行環境慣例」以精簡文字附加到每位專家的 system prompt 尾端。

為什麼需要：repo 慣例（禁裸 python、timeout 前綴、別重複重讀）原本只存在 CLAUDE.md（給
開發 Ti 的 AI 看）與零散補償機制，專家並未被注入——實證（session ap899f8bbeb7 驗屍）：
同一場 session 混用 `python`/`python3`/`.venv/bin/python` 三種寫法、`git status` 跑 64 次、
同檔整檔重讀 27 次。慣例卡是「執行環境」動態文本（依 cwd 分層），與 roles._COMMON 的
「角色身分」靜態文本職責分離——刻意不動 _COMMON（role_store.builtin_body 的 removeprefix
往返依賴其原文）。

注入點：四個 Expert 類（Claude/OpenAI/Codex/Antigravity）__init__ 開頭 `conventions.apply()`
——涵蓋 make_expert 工廠與 autopilot 直接建構（調查分流/自評/拆分）的所有路徑；無工具角色
（complete_once 的 oneshot 反思）不注入。旋鈕 TI_CONVENTIONS_CARD（預設開，0=完全回舊行為）。
"""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path

from . import config
from .roles import Role, effective_tools

# 卡文行數硬上限：慣例卡是「速查」不是文件——要超過上限就該把內容搬去 skills/NOTES，
# 不是把卡養肥（守門測試釘死此值）。
MAX_LINES = 30

_GENERIC = """【執行慣例（務必遵守）】
- 工作目錄有 .venv/bin/python 時，Python 相關指令一律 `.venv/bin/python -m <pytest|ruff|pip>`；禁止裸用 `python`、`python3`、`pytest`、`ruff`。
- 可能久跑的指令一律加 `timeout 60` 前綴（跑測試可放寬為 `timeout 300`）。
- 已讀過的檔案與 `git status` 結果記住並複用；同一場對話不要整檔重讀、不要重複跑同樣的查詢，需要時只讀相關行段。
- 只在工作目錄內建立/修改檔案；結論類交付直接寫在發言裡，不要落檔到 $TMPDIR 或任何暫存路徑。"""

_TI = """【本專案（Ti）速查】
- 測試：`timeout 300 .venv/bin/python -m pytest -q`；子系統目錄 tests/autopilot、tests/core、tests/server、tests/docs；pytest marker 只有 `realgit`。
- lint：`.venv/bin/python -m ruff check .` 與 `... ruff format --check .`；ruff 釘 0.14.4（pyproject/CI/pre-commit 三端同版，勿擅自升版）。
- config 旋鈕須在 studio/config.py 頂層與 reload() 區塊兩處同步定義。"""


def _is_ti_repo(cwd: Path) -> bool:
    """cwd 是不是 Ti 主 repo（pyproject name=ti-studio）。輕量正則即可，不用 tomllib 全解析。"""
    try:
        text = (Path(cwd) / "pyproject.toml").read_text(encoding="utf-8")
    except OSError:
        return False
    return re.search(r'^name\s*=\s*"ti-studio"', text, re.M) is not None


def card(cwd: Path) -> str:
    """組慣例卡（通用段＋Ti 專屬段依 cwd 分層）；旋鈕關閉回空字串。"""
    if not config.CONVENTIONS_CARD:
        return ""
    text = _GENERIC
    if _is_ti_repo(cwd):
        text += "\n" + _TI
    return text


def apply(role: Role, cwd: Path) -> Role:
    """把慣例卡附加到角色 system prompt 尾端，回傳新 Role（原 Role 不變，frozen replace）。

    無工具角色（如 complete_once 的 oneshot 反思、純文字收斂角色）不注入——沒有執行環境
    可言，注入只會污染提示。冪等：已含卡文標頭者不重複附加（防未來某路徑重複 apply）。
    """
    text = card(cwd)
    if not text or not effective_tools(role):
        return role
    if "【執行慣例" in role.system_prompt:
        return role
    return dataclasses.replace(role, system_prompt=role.system_prompt + "\n\n" + text)
