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
    EXPERT_JOINED = "expert_joined"  # PM 動態招募新成員加入 roster（前端動態插入成員欄）
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
    WORKFLOW_PLAN = "workflow_plan"  # 動態流程定義快照（stage 序列），開場廣播供前端與重播
    AGENDA_PLAN = "agenda_plan"  # 拆解結果快照（議程子題、任務、分派表），入 history 供重看
    CONCLUSION = "conclusion"  # 結論彙整：一場討論收斂後產出 CONCLUSION.md 的終局快照
    HUDDLE = "huddle"  # 卡關討論（任務連續失敗時召集團隊找替代方案）
    CRITIC_REVIEW = "critic_review"  # 異議檢查（放行前由獨立 critic 挑錯，防錯誤共識）
    DISPATCH_DECISION = "dispatch_decision"  # 額度感知 per-task 派工（任務暫換 provider/model）
    RETROSPECTIVE = "retrospective"  # 檢討回顧
    TOKEN_USAGE = "token_usage"  # LLM 呼叫 token / cost 用量
    DONE = "done"  # 專案完成
    ERROR = "error"  # 錯誤
    VOTE_RESULT = "vote_result"  # 3-AI 表決結果（PM 無法決定時跨 provider 多數決）
    APPRAISAL = "appraisal"  # 考核：收尾檢討時 PM 對參與 AI 的績效評分（1–5 分＋評語）
    TASK_RESULT = "task_result"  # 任務收尾結果事件


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


def expert_joined(
    session_id: str,
    role_key: str,
    name: str,
    avatar: str,
    title: str,
    tags: list[str],
    provider: str,
    reason: str = "",
) -> StudioEvent:
    """PM 動態招募新成員：前端據此把新角色插入成員欄（roster）並渲染其狀態燈。

    payload 與 SESSION_STARTED 的 roster 條目同形（key/name/avatar/title/tags），另含
    ``provider``（綁到哪個 provider，混合模式可觀測）與 ``reason``（招募緣由，如「庫招募」/「液生」）。
    """
    return StudioEvent(
        EventType.EXPERT_JOINED,
        session_id,
        {
            "key": role_key,
            "name": name,
            "avatar": avatar,
            "title": title,
            "tags": tags,
            "provider": provider,
            "reason": reason,
        },
    )


def tool_use(session_id: str, speaker_key: str, tool: str, summary: str) -> StudioEvent:
    return StudioEvent(
        EventType.TOOL_USE,
        session_id,
        {"speaker": speaker_key, "tool": tool, "summary": summary},
    )


def token_usage(
    session_id: str,
    speaker_key: str,
    provider: str,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    total_tokens: int,
    *,
    cost_usd: float | None = None,
    cache_read: int = 0,
    cache_write: int = 0,
    task_id: int | None = None,
) -> StudioEvent:
    payload = {
        "speaker": speaker_key,
        "provider": provider,
        "model": model,
        "prompt_tokens": int(prompt_tokens or 0),
        "completion_tokens": int(completion_tokens or 0),
        "total_tokens": int(total_tokens or 0),
        "cost_usd": cost_usd,
        "cache_read": int(cache_read or 0),
        "cache_write": int(cache_write or 0),
    }
    if task_id is not None:
        payload["task_id"] = task_id
    return StudioEvent(
        EventType.TOKEN_USAGE,
        session_id,
        payload,
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


def dispatch_decision(
    session_id: str,
    task_id: int,
    title: str,
    role: str,
    provider: str,
    model: str,
    reason: str,
) -> StudioEvent:
    """額度感知 per-task 派工：任務 #task_id 的實作者（role）暫時換綁 provider/model 的決策快照。

    ``model`` 空字串＝沿用該 provider 的預設模型槽；``reason`` 為 flow.choose_dispatch 的
    繁中一句話決策理由（前端以 log-line 顯示、入 history 供重播）。
    """
    return StudioEvent(
        EventType.DISPATCH_DECISION,
        session_id,
        {
            "task_id": task_id,
            "title": title,
            "role": role,
            "provider": provider,
            "model": model,
            "reason": reason,
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


def workflow_plan(session_id: str, name: str, stages: list[dict]) -> StudioEvent:
    """動態流程定義快照：本場採用的 workflow 名稱與 stage 序列（每筆含 type 與顯示名）。

    開場（SESSION_STARTED 後）廣播一次，經既有 broadcast→record_event 入 history，
    供前端呈現流程地圖與事後重播。stages 為已驗證正規化的 dict 列表（直接序列化）。
    """
    return StudioEvent(
        EventType.WORKFLOW_PLAN,
        session_id,
        {"name": name, "stages": stages},
    )


def agenda_plan(
    session_id: str,
    agenda: list[dict],
    tasks: list[dict],
    assignments: list[dict],
    *,
    corrections: list[dict] | None = None,
    edges: list | None = None,
) -> StudioEvent:
    """拆解結果快照：議程（含每子題 title/description/criteria/assignee）、任務清單、
    分派表（assignments: [{index, title, assignee}]，index 1-based）。

    經既有 broadcast→record_event 管道入 history jsonl，供事後重看。
    corrections 為 validate_assignees 的修正紀錄（[{index, given, assigned}]，index
    0-based 對齊 agenda 序）；edges 為任務依賴邊 [(after, before)]，序列化為 list。
    """
    return StudioEvent(
        EventType.AGENDA_PLAN,
        session_id,
        {
            "agenda": agenda,
            "tasks": tasks,
            "assignments": assignments,
            "corrections": corrections or [],
            "edges": [list(e) for e in (edges or [])],
        },
    )


def conclusion(session_id: str, path: str, summary: dict) -> StudioEvent:
    """結論彙整快照：一場討論收斂後落盤 CONCLUSION.md 的通知事件。

    ``path`` 為落盤檔案路徑（事實來源為檔案本身，事件僅為通知）；``summary`` 為
    四鍵結論 dict（consensus/disagreements/open_questions/actions），供前端直接呈現
    而不必再讀檔。經既有 broadcast→record_event 管道入 history。
    """
    return StudioEvent(
        EventType.CONCLUSION,
        session_id,
        {"path": path, "summary": summary},
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


def vote_result(
    session_id: str,
    topic: str,
    options: list[str],
    ballots: list[dict],
    winner: str,
    tie: bool,
    degraded: bool = False,
) -> StudioEvent:
    """3-AI 表決結果：PM 無法決定時，找兩位不同 provider 的 AI 與 PM 多數決的終局快照。

    ``ballots``＝``[{voter, provider, choice}]``（choice 空字串＝棄權）；``tie``＝最高票
    平手（此時以 PM 票定案）；``degraded``＝可用外部 provider 不足兩位、未建投票員、
    退化為 PM 單票定案。經既有 broadcast→record_event 管道入 history 供重播。
    """
    return StudioEvent(
        EventType.VOTE_RESULT,
        session_id,
        {
            "topic": topic,
            "options": options,
            "ballots": ballots,
            "winner": winner,
            "tie": tie,
            "degraded": degraded,
        },
    )


def appraisal(
    session_id: str,
    provider: str,
    model: str,
    role: str,
    score: int,
    comment: str,
) -> StudioEvent:
    """一筆 AI 成員考核：收尾檢討時 PM 對參與者打的 1–5 分（5 最佳）＋一句評語。

    ``provider``／``role`` 至少一者非空（PM 以 provider 名或在場 role key 指認對象）；
    ``model`` 可為空字串（未知或該 provider 預設模型槽）。前端以 log-line 顯示、經既有
    broadcast→record_event 入 history 供重播；持久化聚合另走 studio/appraisal 考核庫。
    """
    return StudioEvent(
        EventType.APPRAISAL,
        session_id,
        {
            "provider": provider,
            "model": model,
            "role": role,
            "score": int(score),
            "comment": comment,
        },
    )


def task_result(
    session_id: str,
    task_id: int,
    role: str,
    provider: str,
    model: str | None,
    duration_s: float | None,
    qa_rounds: int | None,
    input_tokens: int | None,
    output_tokens: int | None,
    total_tokens: int | None,
    cost_usd: float | None,
    cost_source: str | None,
) -> StudioEvent:
    """單一任務執行結果：包含耗時、QA 輪數、LLM token 及費用等。

    消費端可用 task_id 與 dispatch_decision 進行 join。
    """
    return StudioEvent(
        EventType.TASK_RESULT,
        session_id,
        {
            "task_id": task_id,
            "role": role,
            "provider": provider,
            "model": model,
            "duration_s": duration_s,
            "qa_rounds": qa_rounds,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
            "cost_usd": cost_usd,
            "cost_source": cost_source,
        },
    )
