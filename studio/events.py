"""工作室事件 — Orchestrator 產生、透過 WebSocket 即時送到前端的訊息。"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class EventType(str, Enum):
    SESSION_STARTED = "session_started"  # 工作室開工
    PHASE_CHANGE = "phase_change"  # 進入新階段（拆解/實作/驗證/審查/檢討…）
    EXPERT_MESSAGE = "expert_message"  # 某位專家發言（可為串流片段）
    EXPERT_STATUS = "expert_status"  # 專家狀態燈（idle/thinking/working）
    TOOL_USE = "tool_use"  # 專家使用工具（寫檔/執行指令…）
    BOARD_UPDATE = "board_update"  # 看板整體更新
    TASK_STATUS = "task_status"  # 單一任務狀態變更
    RUN_RESULT = "run_result"  # 測試/執行結果（PASS/FAIL）
    DEMO_RESULT = "demo_result"  # 實際執行產出（含 stdout/stderr）
    GIT_COMMIT = "git_commit"  # workspace 內階段性 commit
    PUBLISH_RESULT = "publish_result"  # 成果發佈到 GitHub 的結果
    CI_RESULT = "ci_result"  # 發佈後 CI/CD 驗證與自動合併的進度
    HUMAN_MESSAGE = "human_message"  # 人類中途插話
    CLARIFY_REQUEST = "clarify_request"  # 需求澄清：PM 向使用者反問關鍵問題（附預設假設）
    HUDDLE = "huddle"  # 卡關討論（任務連續失敗時召集團隊找替代方案）
    CRITIC_REVIEW = "critic_review"  # 異議檢查（放行前由獨立 critic 挑錯，防錯誤共識）
    RETROSPECTIVE = "retrospective"  # 檢討回顧
    DONE = "done"  # 專案完成
    ERROR = "error"  # 錯誤


@dataclass
class StudioEvent:
    type: EventType
    session_id: str
    payload: dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type.value,
            "session_id": self.session_id,
            "ts": self.ts,
            "payload": self.payload,
        }


# --- 建構小幫手 ---------------------------------------------------------


def expert_message(
    session_id: str,
    speaker_key: str,
    name: str,
    avatar: str,
    text: str,
    *,
    streaming: bool = False,
    final: bool = False,
) -> StudioEvent:
    return StudioEvent(
        EventType.EXPERT_MESSAGE,
        session_id,
        {
            "speaker": speaker_key,
            "name": name,
            "avatar": avatar,
            "text": text,
            "streaming": streaming,
            "final": final,
        },
    )


def expert_status(session_id: str, speaker_key: str, status: str) -> StudioEvent:
    return StudioEvent(
        EventType.EXPERT_STATUS, session_id, {"speaker": speaker_key, "status": status}
    )


def tool_use(session_id: str, speaker_key: str, tool: str, summary: str) -> StudioEvent:
    return StudioEvent(
        EventType.TOOL_USE,
        session_id,
        {"speaker": speaker_key, "tool": tool, "summary": summary},
    )


def phase_change(session_id: str, phase: str, detail: str = "") -> StudioEvent:
    return StudioEvent(EventType.PHASE_CHANGE, session_id, {"phase": phase, "detail": detail})


def board_update(session_id: str, columns: dict[str, list[dict]]) -> StudioEvent:
    return StudioEvent(EventType.BOARD_UPDATE, session_id, {"columns": columns})


def run_result(session_id: str, passed: bool, detail: str, log: str = "") -> StudioEvent:
    return StudioEvent(
        EventType.RUN_RESULT,
        session_id,
        {"passed": passed, "detail": detail, "log": log},
    )


def demo_result(
    session_id: str, command: str, exit_code: int, output: str, *, label: str = "Demo"
) -> StudioEvent:
    return StudioEvent(
        EventType.DEMO_RESULT,
        session_id,
        {
            "label": label,
            "command": command,
            "exit_code": exit_code,
            "passed": exit_code == 0,
            "output": output,
        },
    )


def git_commit(session_id: str, message: str, commit_hash: str) -> StudioEvent:
    return StudioEvent(EventType.GIT_COMMIT, session_id, {"message": message, "hash": commit_hash})


def human_message(session_id: str, text: str) -> StudioEvent:
    return StudioEvent(EventType.HUMAN_MESSAGE, session_id, {"text": text})


def clarify_request(session_id: str, questions: list[dict], timeout_s: float) -> StudioEvent:
    """PM 的需求澄清提問。questions: [{"q": 問題, "assumption": 無回覆時的預設假設}]。"""
    return StudioEvent(
        EventType.CLARIFY_REQUEST,
        session_id,
        {"questions": questions, "timeout_s": timeout_s},
    )


def publish_result(session_id: str, result: dict) -> StudioEvent:
    return StudioEvent(EventType.PUBLISH_RESULT, session_id, result)


def ci_result(session_id: str, payload: dict) -> StudioEvent:
    """發佈後 CI/CD 驗證與合併進度。

    payload.state ∈ {pass, fail, none, error, merged, merge_failed, giveup}；
    另含 attempt/rounds/detail（視階段而定）、merged（bool，合併是否成功）。
    """
    return StudioEvent(EventType.CI_RESULT, session_id, payload)


def huddle(
    session_id: str,
    task_id: int,
    title: str,
    participants: list[str],
    conclusion: str,
    *,
    limitation: bool = False,
) -> StudioEvent:
    """卡關討論事件。limitation=True 代表 huddle 後仍未解決、標記為『已知限制』。"""
    return StudioEvent(
        EventType.HUDDLE,
        session_id,
        {
            "task_id": task_id,
            "title": title,
            "participants": participants,
            "conclusion": conclusion,
            "limitation": limitation,
        },
    )


def critic_review(session_id: str, gate: str, passed: bool, text: str) -> StudioEvent:
    """異議檢查結果。gate 標示視角（如 pm／senior）；passed=False 代表 critic 異議成立、退回。"""
    return StudioEvent(
        EventType.CRITIC_REVIEW,
        session_id,
        {"gate": gate, "passed": passed, "text": text},
    )


def task_status(session_id: str, task_id: int, title: str, status: str) -> StudioEvent:
    return StudioEvent(
        EventType.TASK_STATUS,
        session_id,
        {"id": task_id, "title": title, "status": status},
    )


def error(session_id: str, message: str) -> StudioEvent:
    return StudioEvent(EventType.ERROR, session_id, {"message": message})
