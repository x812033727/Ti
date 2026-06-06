"""給非 Claude provider 用的工具層（OpenAI function-calling）。

Claude Agent SDK 自帶 Read/Write/Edit/Bash；其他模型沒有，所以在這裡用 OpenAI 的
function-calling 規格定義同名工具，並提供實際在 workspace cwd 上執行的 execute()。
"""

from __future__ import annotations

import json
from pathlib import Path

from . import runner

# OpenAI function-calling 工具規格
_SPECS: dict[str, dict] = {
    "read_file": {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "讀取 workspace 內某個檔案的內容",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "相對路徑"}},
                "required": ["path"],
            },
        },
    },
    "write_file": {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "建立或覆寫 workspace 內的檔案",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    },
    "edit_file": {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "把檔案中的一段文字替換成另一段（old 必須唯一）",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old": {"type": "string"},
                    "new": {"type": "string"},
                },
                "required": ["path", "old", "new"],
            },
        },
    },
    "run_bash": {
        "type": "function",
        "function": {
            "name": "run_bash",
            "description": "在 workspace 執行 shell 指令（安裝套件、執行程式、跑測試）",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
        },
    },
}

# 把 Claude 工具名對應到本層工具
_CLAUDE_TO_LOCAL = {
    "Read": ["read_file"],
    "Write": ["write_file"],
    "Edit": ["edit_file"],
    "Bash": ["run_bash"],
}


def specs_for(allowed_claude_tools: list[str]) -> list[dict]:
    """依角色的 Claude 工具清單，回傳對應的 OpenAI 工具規格（read_file 一律提供）。"""
    names = {"read_file"}
    for t in allowed_claude_tools:
        for local in _CLAUDE_TO_LOCAL.get(t, []):
            names.add(local)
    return [_SPECS[n] for n in _SPECS if n in names]


def _safe_path(cwd: Path, rel: str) -> Path | None:
    root = Path(cwd).resolve()
    target = (root / rel).resolve()
    if target != root and root not in target.parents:
        return None
    return target


async def execute(name: str, args: dict, cwd: Path) -> str:
    """執行一個工具呼叫，回傳給模型的文字結果。"""
    cwd = Path(cwd)
    try:
        if name == "read_file":
            target = _safe_path(cwd, args.get("path", ""))
            if not target or not target.is_file():
                return f"錯誤：找不到 {args.get('path')}"
            return target.read_text(encoding="utf-8", errors="replace")

        if name == "write_file":
            target = _safe_path(cwd, args.get("path", ""))
            if not target:
                return "錯誤：路徑超出 workspace"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(args.get("content", ""), encoding="utf-8")
            return f"已寫入 {args.get('path')}"

        if name == "edit_file":
            target = _safe_path(cwd, args.get("path", ""))
            if not target or not target.is_file():
                return f"錯誤：找不到 {args.get('path')}"
            text = target.read_text(encoding="utf-8")
            old = args.get("old", "")
            if text.count(old) != 1:
                return f"錯誤：old 在檔案中出現 {text.count(old)} 次，需唯一"
            target.write_text(text.replace(old, args.get("new", "")), encoding="utf-8")
            return f"已修改 {args.get('path')}"

        if name == "run_bash":
            result = await runner.run_command(cwd, args.get("command", ""))
            return f"exit={result.exit_code}\n{result.output}"

        return f"錯誤：未知工具 {name}"
    except Exception as exc:  # noqa: BLE001
        return f"工具執行錯誤：{type(exc).__name__}: {exc}"


def summarize(name: str, args: dict) -> str:
    """給 UI 顯示的一行摘要。"""
    if name in ("read_file", "write_file", "edit_file"):
        verb = {"read_file": "讀取", "write_file": "寫入", "edit_file": "修改"}[name]
        return f"{verb} {args.get('path', '')}"
    if name == "run_bash":
        return "執行: " + (args.get("command", "")[:120])
    return name


def parse_args(raw: str | dict) -> dict:
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
