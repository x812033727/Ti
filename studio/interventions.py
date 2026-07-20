"""人工介入留痕(第 3 階信任指標 A0):面板寫入操作的結構化紀錄。

第 3 階(監督式自治)的核心指標是「零人工介入合併率」與「介入的性質」——沒有這份留痕,
信任只能憑感覺。routes.py 的人工寫入端點在動作成功後呼叫 record(),分三類:

- output_review(成果審查型):對特定任務/成果下人工判斷(task action、triage)。
  第 3 階要把這類歸零——人不再逐件驗收。
- context_feeding(補背景型):餵任務/改設定——這正是第 3 階人類的職責,不算不信任。
- ops(維運型):pause/resume/派工模式/帳號切換。中性,不進零介入口徑。

未知 category 一律歸 output_review(fail-conservative:寧可低估零介入率,不虛增信任)。
落檔 autopilot/interventions.jsonl(jsonl_log 範式);聚合端在 insights.trust_metrics。
已知限制:繞過面板直接在 GitHub 上的人工操作不可見——口徑是「面板留痕的介入」。
"""

from __future__ import annotations

from pathlib import Path

from . import config, jsonl_log

CATEGORIES = ("output_review", "context_feeding", "ops")

# v1 自治契約使用更精確的四分類；舊 category 欄保留，避免既有看板/報告破壞。
INTERVENTION_TYPES = ("background", "product_decision", "bug_design_fix", "ops_rescue")
_LEGACY_TO_V1 = {
    "context_feeding": "background",
    "output_review": "bug_design_fix",
    "ops": "ops_rescue",
}
_V1_TO_LEGACY = {
    "background": "context_feeding",
    "product_decision": "output_review",
    "bug_design_fix": "output_review",
    "ops_rescue": "ops",
}


def _path(state_dir: Path | None = None) -> Path:
    return (state_dir or config.AUTOPILOT_STATE_DIR) / "interventions.jsonl"


def record(
    kind: str,
    category: str,
    *,
    task_id: int | None = None,
    detail: str = "",
    intervention_type: str = "",
    project_id: str = "unknown",
    run_id: str = "unknown",
    state_dir: Path | None = None,
) -> None:
    """記一筆人工介入;永不拋錯(jsonl_log 吞掉一切)。detail 夾長度防灌爆。"""
    requested_v1 = intervention_type or (category if category in INTERVENTION_TYPES else "")
    if requested_v1 not in INTERVENTION_TYPES:
        requested_v1 = _LEGACY_TO_V1.get(category, "bug_design_fix")
    if category in INTERVENTION_TYPES:
        category = _V1_TO_LEGACY[category]
    elif category not in CATEGORIES:
        category = "output_review"
    rec: dict = {
        "kind": kind,
        "category": category,
        "intervention_type": requested_v1,
        "project_id": project_id or "unknown",
        "run_id": run_id or "unknown",
    }
    if task_id is not None:
        rec["task_id"] = task_id
    if detail:
        rec["detail"] = str(detail)[:200]
    jsonl_log.append(_path(state_dir), rec)
    try:
        from . import autonomy

        autonomy.emit_event(
            "human_intervention",
            run_id=run_id,
            project_id=project_id,
            task_id=task_id if task_id is not None else "unknown",
            intervention_type=requested_v1,
            outcome="recorded",
            payload={"kind": kind, "detail": str(detail)[:200]},
            state_dir=state_dir,
        )
    except Exception:
        # 介入留痕本身不得因新版投影失敗而中斷既有寫入路徑。
        pass


def read_window(days: int, *, state_dir: Path | None = None) -> list[dict]:
    """讀近 days 天的介入紀錄(壞行容錯,檔案不存在=空)。"""
    return jsonl_log.read_window(_path(state_dir), days)
