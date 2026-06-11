"""Reflexion 反思 —— 把一輪失敗的「評審意見」蒸餾成精簡、可操作的文字反思，存進記憶。

供主迴圈在某輪未通過後呼叫：以任務、工程師該輪實作、評審意見為依據，產生一段「為何沒過、
下一輪具體怎麼改」的反思，寫入 memory（task 級），下一輪 prepend 回工程師 context。

守則（對齊既有評審分工）：反思只覆盤、不裁決成敗、不給分數——pass/fail 的唯一來源仍是
QA／高工／客觀閘門。產出保證非空（LLM 不可用或回空時 fallback 成模板反思），且永不 raise——
反思失敗絕不中斷主迴圈。移植自 ti-studio 自我進步交付的 selfimprove/reflexion.py。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from . import memory

# (system, user) -> 反思文字。對齊 providers.complete_once 的前兩個位置參數。
LLMFn = Callable[[str, str], Awaitable[str]]

MAX_REFLECTION_CHARS = 1200  # 單筆反思上限，避免稀釋訊號／撐爆下一輪 context
_IMPL_EXCERPT_CHARS = 1500
_FEEDBACK_EXCERPT_CHARS = 2000

_SYSTEM_PROMPT = (
    "你是一位資深工程師，正在覆盤一次『未通過的任務實作輪次』。請依據任務、你這輪的實作、"
    "以及評審（驗證／審查）給出的意見，寫出一段精簡、可操作的反思，指出『這輪為什麼沒過、"
    "下一輪具體要怎麼改』。\n"
    "規則：\n"
    "1. 只輸出純文字反思，不要輸出程式碼或 markdown 區塊。\n"
    "2. 聚焦根因與下一步修正方向，不要複述整個任務。\n"
    "3. 不要宣稱通過與否、不要給分數——成敗由外部評審決定，不歸你判定。\n"
    "4. 控制在 120 字以內，越具體越好（點出該檢查的邊界／資料結構／邏輯）。"
)


def _excerpt(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit] + "…（略）"


def build_prompt(task_title: str, round_no: int, impl_text: str, feedback: str) -> tuple[str, str]:
    """回傳 (system, user) 兩段 prompt。"""
    user = (
        f"# 任務\n{task_title.strip()}\n\n"
        f"# 我這輪（第 {round_no} 輪）的實作摘要\n{_excerpt(impl_text, _IMPL_EXCERPT_CHARS)}\n\n"
        f"# 評審意見（未通過原因）\n{_excerpt(feedback, _FEEDBACK_EXCERPT_CHARS)}\n\n"
        "請寫出一段反思，說明失敗根因與下一輪的具體修正方向。"
    )
    return _SYSTEM_PROMPT, user


def _fallback_text(feedback: str) -> str:
    """LLM 不可用或回空時的保底反思（仍以評審意見為依據，保證非空）。"""
    fb = (feedback or "").strip()
    head = _excerpt(fb, 300) if fb else "上一輪未通過評審。"
    return (
        f"上一輪未通過，評審指出：{head} "
        "下一輪需針對此逐項修正，並重新確認邊界條件與輸出是否符合驗收標準。"
    )


def _normalize(text: str, feedback: str) -> str:
    text = (text or "").strip()
    if not text:
        text = _fallback_text(feedback)
    if len(text) > MAX_REFLECTION_CHARS:
        text = text[:MAX_REFLECTION_CHARS].rstrip() + "…"
    return text


async def reflect_and_store(
    session_id: str,
    task: dict,
    round_no: int,
    impl_text: str,
    feedback: str,
    *,
    llm: LLMFn,
) -> str:
    """蒸餾反思並寫入記憶，回傳反思文字。

    全程 try/except：LLM 拋錯／逾時／回空都退回模板，保證寫入非空且永不 raise。
    """
    task_id = task.get("id")
    task_title = f"#{task_id}：{task.get('title', '')}"
    system, user = build_prompt(task_title, round_no, impl_text, feedback)
    try:
        raw = await llm(system, user)
    except Exception:
        raw = ""
    text = _normalize(raw, feedback)
    try:
        memory.write(
            session_id,
            task_id,
            f"[第 {round_no} 輪反思] {text}",
            round_no=round_no,
            meta={"round": round_no},
        )
    except Exception:
        pass  # 記憶寫入失敗也不該中斷主迴圈
    return text
