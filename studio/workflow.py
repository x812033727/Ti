"""動態流程（Workflow）：把工作室原本寫死在 ``StudioSession._run()``／``_work_task()`` 的
固定骨架，改成「一份宣告式 stage 序列」驅動，讓全流程（架構討論→任務執行→整合→Demo→
發佈）都能依需求裁剪、換參與者、插入「動態 step」（PM 運行時決定下一步找誰）。

設計沿用既有兩個成熟範式：
- **資料面**鏡射 ``role_store`` 的討論小組（Group）區段——單檔 ``<ROLES_DIR>/workflows.yaml``、
  頂層 ``workflows:`` 列表、temp＋rename 原子寫、寫入時硬驗證（違反 raise ``WorkflowError``，
  API 層轉 422）、即時讀檔（測試 monkeypatch ``config.ROLES_DIR`` 後立即生效）。
- **流程面**只引用 ``flow.py`` 既有的收斂判定函式（白名單 ``VERDICTS``），workflow 不得注入
  任意程式碼；客觀閘門／stall guard／wind-down 等引擎不變式刻意不可被 workflow 配置掉。

單一真相：``default_workflow()`` 是「等價於現有寫死骨架」的程式碼內建定義（不存檔）。
``StudioSession(workflow=None)`` ＝載入它 → 走同一段直譯器、同一順序 → 與重構前等價，
autopilot／improver／既有 session 行為不變。客製 workflow 僅改動順序／參與者／插 dynamic step。

Schema（兩層）
--------------
Session 級 stage（``STAGE_TYPES``）：clarify／research／decompose／discuss／build／
integrate／demo／wrap_up／publish／dynamic。
Task 級 stage（``TASK_STAGE_TYPES``，內嵌於 ``build.task_pipeline``）：implement／review／
gate／dynamic。

Stage 欄位（pydantic ``extra="forbid"``，未知欄位明確報錯）：
    type        必填，須為對應層級的合法型別
    name        選填，事件 phase 顯示名（預設用型別）
    roles       選填 list[role_key]（discuss／review／dynamic 的參與者；缺省＝沿用內建選角）
    assignee    選填 單一 role_key（implement／單人 stage）
    mode        選填 round_robin｜parallel｜single（預設依型別）
    gate        選填 list[{role, verdict, optional}]，verdict ∈ VERDICTS 白名單
    max_rounds  選填 int≥0（0＝取對應 config 旋鈕）
    optional    bool，角色不在場／when 不成立則整個 stage 跳過（預設 False）
    when        選填條件 token：``has:<role_key>``／``flag:<config 旗標>``
    budget      dynamic 專用，最大 hop 數 int≥0（0＝取 config.DYNAMIC_STEP_BUDGET）
    fallback    dynamic 專用，PM 給不出合法下一步時的退路 role（預設 engineer）
    task_pipeline  僅 build 可有（且必填）：task 級 stage 列表

寫入硬規則（違反 raise WorkflowError）
- name 非空、≤64 字元；stages 非空。
- 每個 stage type 屬於對應層級白名單；mode（若給）∈ STAGE_MODES。
- 一切引用到的角色（roles／assignee／gate.role／fallback）須存在於 ``roles.BY_KEY``。
- gate.verdict ∈ VERDICTS；max_rounds／budget ≥0。
- ``build`` 必含非空 ``task_pipeline``；其餘 stage 不得有 task_pipeline。
- ``when`` 須符合 ``has:<key>``／``flag:<NAME>`` 格式。

主要介面（鏡射 role_store 的 Group 區段）
- ``default_workflow() -> dict``：內建預設（等價現有骨架，單一真相）。
- ``list_workflows() -> list[dict]``：全部 workflow（檔案不存在＝[]；壞檔 raise WorkflowFileError）。
- ``get_workflow(name) -> dict | None``：檔案優先；命中不到且 name 為預設名 → default_workflow()。
- ``create_workflow / update_workflow / delete_workflow``：CRUD（同名→None→409、不存在→None/False→404）。
- ``validate_workflow(name, description, stages) -> dict``：驗證並正規化；不合法 raise WorkflowError。
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError

from . import config, flow, roles

logger = logging.getLogger("ti.workflow")

# --- 白名單（單一真相，schema 驗證與直譯器共用）---------------------------

# Session 級 stage 型別。順序即「預設骨架」的自然順序（僅供閱讀，實際順序由定義決定）。
STAGE_TYPES = (
    "clarify",
    "research",
    "decompose",
    "discuss",
    "build",
    "integrate",
    "demo",
    "wrap_up",
    "publish",
    "dynamic",
)

# Task 級 stage 型別（內嵌於 build.task_pipeline）。
TASK_STAGE_TYPES = ("implement", "review", "gate", "dynamic")

# 發言調度模式（沿用 discussion / 三審並行的既有語意）。
STAGE_MODES = ("round_robin", "parallel", "single")

# 收斂判定白名單：verdict 名稱 → flow.py 既有純函式。workflow 只能引用這些，
# 不得注入任意程式碼（反 reward-hacking：客觀閘門等引擎不變式不在此清單、不可配置）。
VERDICTS: dict[str, object] = {
    "qa_passed": flow.qa_passed,
    "senior_approved": flow.senior_approved,
    "security_approved": flow.security_approved,
    "critic_blocks": flow.critic_blocks,
    "pm_done": flow.pm_done,
}

# 內建 workflow 的保留名稱：get_workflow 命中不到時回對應內建定義；不可被同名檔案覆蓋。
DEFAULT_WORKFLOW_NAME = "預設流程"  # 等價現有寫死骨架
DYNAMIC_FIRST_NAME = "動態優先"  # dynamic-first：PM 運行時溝通/分派/招募為主（互動預設）
# 全部保留名（不可被使用者建立/覆寫；list_workflows 一律前置供 UI 可選）。
RESERVED_NAMES = (DEFAULT_WORKFLOW_NAME, DYNAMIC_FIRST_NAME)

WORKFLOWS_FILENAME = "workflows.yaml"

# workflow 名稱：非空、≤64 字（顯示名兼 workflows.yaml 內的查詢鍵，不作檔名用）。
_WORKFLOW_NAME_MAX = 64

# when 條件 token：has:<role_key>（角色在場）／flag:<CONFIG_NAME>（config 旗標為真）。
_WHEN_RE = re.compile(r"^(has|flag):[A-Za-z][A-Za-z0-9_]*$")


class WorkflowError(ValueError):
    """workflow 定義不合法（型別／角色／verdict／結構），訊息為人讀原因。"""


class WorkflowFileError(ValueError):
    """workflows.yaml 本身損壞（YAML 解析失敗或結構不符），訊息為人讀原因。"""


# --- pydantic 模型（欄位層級驗證；語意層級驗證在 validate_workflow）----------


class GateClause(BaseModel):
    """review／gate stage 的單條閘門：某角色的某 verdict 須成立。"""

    model_config = ConfigDict(extra="forbid")

    role: str
    verdict: str
    optional: bool = False


class Stage(BaseModel):
    """單一 stage。同一模型兼任 session 級與 task 級——層級合法性在 validate_workflow 判定。"""

    model_config = ConfigDict(extra="forbid")

    type: str
    name: str = ""
    roles: list[str] = []
    assignee: str = ""
    mode: str = ""
    gate: list[GateClause] = []
    max_rounds: int = 0
    optional: bool = False
    when: str = ""
    budget: int = 0
    fallback: str = "engineer"
    task_pipeline: list[Stage] = []


class WorkflowModel(BaseModel):
    """整份 workflow（name＋description＋stages）的欄位層級驗證模型。"""

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str = ""
    stages: list[Stage]


def _validation_reasons(e: ValidationError) -> str:
    """把 pydantic 冗長錯誤壓成單行人讀原因（含未知欄位／缺必填欄位的明確指名）。"""
    return "; ".join(
        f"{'.'.join(str(p) for p in err['loc']) or '<root>'}: {err['msg']}" for err in e.errors()
    )


def _check_role(key: str, where: str) -> None:
    """角色存在性硬驗證：key 必須在 roles.BY_KEY（與 Group 同一套規則）。"""
    if key not in roles.BY_KEY:
        raise WorkflowError(f"{where} 引用不存在的角色 {key!r}（可用角色見 GET /api/roles）")


def _validate_stage(stage: Stage, *, task_level: bool, path: str) -> dict:
    """驗證單一 stage 並回傳正規化 dict；不合法 raise WorkflowError。

    task_level=True 時型別取 TASK_STAGE_TYPES（且不得再有 task_pipeline）。
    """
    allowed = TASK_STAGE_TYPES if task_level else STAGE_TYPES
    if stage.type not in allowed:
        raise WorkflowError(
            f"{path}: stage type {stage.type!r} 不合法（{'task' if task_level else 'session'} "
            f"級允許 {allowed}）"
        )

    if stage.mode and stage.mode not in STAGE_MODES:
        raise WorkflowError(f"{path}: mode 須為 {STAGE_MODES} 之一，收到 {stage.mode!r}")

    if stage.max_rounds < 0:
        raise WorkflowError(f"{path}: max_rounds 須 ≥0，收到 {stage.max_rounds}")

    if stage.budget < 0:
        raise WorkflowError(f"{path}: budget 須 ≥0，收到 {stage.budget}")

    for k in stage.roles:
        _check_role(k, f"{path}.roles")
    if stage.assignee:
        _check_role(stage.assignee, f"{path}.assignee")

    for i, clause in enumerate(stage.gate):
        _check_role(clause.role, f"{path}.gate[{i}].role")
        if clause.verdict not in VERDICTS:
            raise WorkflowError(
                f"{path}.gate[{i}]: verdict {clause.verdict!r} 不在白名單 {tuple(VERDICTS)}"
            )

    if stage.when and not _WHEN_RE.match(stage.when):
        raise WorkflowError(
            f"{path}: when {stage.when!r} 格式不符（須為 has:<role_key> 或 flag:<CONFIG_NAME>）"
        )

    if stage.type == "dynamic":
        # fallback 一律有預設 engineer；只要它存在即可（避免給不出合法下一步時無路可退）。
        _check_role(stage.fallback, f"{path}.fallback")

    pipeline_out: list[dict] = []
    if stage.type == "build":
        if task_level:
            raise WorkflowError(f"{path}: build 不可巢狀於 task_pipeline 內")
        if not stage.task_pipeline:
            raise WorkflowError(f"{path}: build stage 必須含非空 task_pipeline")
        for i, sub in enumerate(stage.task_pipeline):
            pipeline_out.append(
                _validate_stage(sub, task_level=True, path=f"{path}.task_pipeline[{i}]")
            )
    elif stage.task_pipeline:
        raise WorkflowError(f"{path}: 只有 build stage 可有 task_pipeline")

    out: dict = {"type": stage.type}
    # 只輸出「非預設」欄位，讓 workflows.yaml 精簡、往返穩定（預設 workflow 序列化後仍可讀）。
    if stage.name:
        out["name"] = stage.name
    if stage.roles:
        out["roles"] = list(stage.roles)
    if stage.assignee:
        out["assignee"] = stage.assignee
    if stage.mode:
        out["mode"] = stage.mode
    if stage.gate:
        out["gate"] = [
            {"role": c.role, "verdict": c.verdict, **({"optional": True} if c.optional else {})}
            for c in stage.gate
        ]
    if stage.max_rounds:
        out["max_rounds"] = stage.max_rounds
    if stage.optional:
        out["optional"] = True
    if stage.when:
        out["when"] = stage.when
    if stage.type == "dynamic":
        if stage.budget:
            out["budget"] = stage.budget
        out["fallback"] = stage.fallback
    if pipeline_out:
        out["task_pipeline"] = pipeline_out
    return out


def validate_workflow(name: str, description: str, stages: list) -> dict:
    """驗證並正規化一份 workflow，回傳 ``{name, description, stages}``；不合法 raise WorkflowError。

    stages 接受 dict 列表（API／YAML 來源）或 Stage 物件列表（內建來源）。
    """
    name = (name or "").strip()
    if not name:
        raise WorkflowError("workflow name 不可為空")
    if len(name) > _WORKFLOW_NAME_MAX:
        raise WorkflowError(f"workflow name 過長（≤{_WORKFLOW_NAME_MAX} 字元）")

    try:
        model = WorkflowModel.model_validate(
            {"name": name, "description": description or "", "stages": stages or []}
        )
    except ValidationError as e:
        raise WorkflowError(f"欄位驗證失敗：{_validation_reasons(e)}") from e

    if not model.stages:
        raise WorkflowError("workflow 至少需一個 stage")

    stages_out = [
        _validate_stage(s, task_level=False, path=f"stages[{i}]")
        for i, s in enumerate(model.stages)
    ]
    return {"name": model.name, "description": model.description, "stages": stages_out}


# --- 內建預設 workflow（單一真相，等價現有寫死骨架）------------------------


def default_workflow() -> dict:
    """等價於現有 ``_run()``／``_work_task()`` 寫死骨架的內建定義（不存檔）。

    ``StudioSession(workflow=None)`` 載入它；直譯器對 default 走的 handler 與重構前同一段碼、
    同一順序。discuss 不硬指定 roles → 沿用既有 group／architect／DISCUSS_MODE 選角；
    research／integrate 以 ``when`` 重現「角色缺席就跳過」。
    """
    return {
        "name": DEFAULT_WORKFLOW_NAME,
        "description": "等價於現有寫死骨架：澄清→調研→拆解→架構討論→任務波次→整合→Demo→驗收→發佈",
        "stages": [
            {"type": "clarify"},
            {"type": "research", "optional": True, "when": "has:researcher"},
            {"type": "decompose"},
            {"type": "discuss"},
            {
                "type": "build",
                "task_pipeline": [
                    {"type": "implement", "assignee": "engineer"},
                    {
                        "type": "review",
                        "mode": "parallel",
                        "gate": [
                            {"role": "qa", "verdict": "qa_passed"},
                            {"role": "senior", "verdict": "senior_approved"},
                            {"role": "security", "verdict": "security_approved", "optional": True},
                        ],
                    },
                    {
                        "type": "gate",
                        "roles": ["pm"],
                        "gate": [{"role": "pm", "verdict": "critic_blocks"}],
                    },
                ],
            },
            {"type": "integrate", "optional": True, "when": "has:devops"},
            {"type": "demo"},
            {"type": "wrap_up"},
            {"type": "publish"},
        ],
    }


def dynamic_first_workflow() -> dict:
    """dynamic-first 內建流程（互動 session 預設）：以 PM 運行時溝通/分派/招募為主。

    與預設骨架的差異：把固定的架構辯論（discuss）換成 session 級 `dynamic`（PM 動態溝通、依
    額度分派、可招募新人），並在任務 pipeline 末加 task 級 `dynamic`（PM 動態追加把關）。
    保留全部安全閘門（review／critic／客觀閘門／demo 驗證／wrap_up 檢討＝改善計畫）。
    不存檔、保留名、不可被覆寫；autopilot／improver 不走此流程（維持安全骨架）。
    """
    return {
        "name": DYNAMIC_FIRST_NAME,
        "description": (
            "動態為主：PM 運行時溝通/分派/招募（額度感知）＋標準審查＋驗證＋改善計畫（互動 session 預設）"
        ),
        "stages": [
            {"type": "clarify"},
            {"type": "research", "optional": True, "when": "has:researcher"},
            {"type": "decompose"},
            {"type": "dynamic", "name": "動態溝通與分派", "budget": 5, "fallback": "engineer"},
            {
                "type": "build",
                "task_pipeline": [
                    {"type": "implement", "assignee": "engineer"},
                    {
                        "type": "review",
                        "mode": "parallel",
                        "gate": [
                            {"role": "qa", "verdict": "qa_passed"},
                            {"role": "senior", "verdict": "senior_approved"},
                            {"role": "security", "verdict": "security_approved", "optional": True},
                        ],
                    },
                    {
                        "type": "gate",
                        "roles": ["pm"],
                        "gate": [{"role": "pm", "verdict": "critic_blocks"}],
                    },
                    {"type": "dynamic", "budget": 2, "fallback": "engineer"},
                ],
            },
            {"type": "integrate", "optional": True, "when": "has:devops"},
            {"type": "demo"},
            {"type": "wrap_up"},
            {"type": "publish"},
        ],
    }


# 保留名 → 內建定義工廠（get_workflow／list_workflows 用）。
_BUILTIN_WORKFLOWS = {
    DEFAULT_WORKFLOW_NAME: default_workflow,
    DYNAMIC_FIRST_NAME: dynamic_first_workflow,
}


def coerce(workflow: dict | None) -> dict:
    """把 ``StudioSession(workflow=...)`` 收到的值正規化成驗證過的 workflow dict。

    None → default_workflow()。dict → 重新 validate（防外部繞過 store 塞進非法定義）。
    驗證失敗時退回 default 並 log（執行期不因壞 workflow 崩潰；寫入期才硬擋）。
    """
    if workflow is None:
        return default_workflow()
    try:
        return validate_workflow(
            workflow.get("name", ""), workflow.get("description", ""), workflow.get("stages", [])
        )
    except WorkflowError as e:
        logger.warning("workflow %r 不合法（%s），改用預設流程", workflow.get("name"), e)
        return default_workflow()


# =========================================================================
# 檔案 store：workflows.yaml（頂層 workflows: 列表），鏡射 role_store 的 Group 區段
# =========================================================================


def workflows_path() -> Path:
    """workflows.yaml 的完整路徑（即時讀 config.ROLES_DIR，測試 monkeypatch 後立即生效）。"""
    return Path(config.ROLES_DIR) / WORKFLOWS_FILENAME


def list_workflows() -> list[dict]:
    """讀取全部 workflow。檔案不存在＝[]；YAML 壞掉或結構不符 raise WorkflowFileError。

    讀取端「不」重驗角色是否仍存在（角色檔可能事後被刪）——存在性只在寫入時強制，
    讀回忠實呈現檔案內容。
    """
    path = workflows_path()
    if not path.is_file():
        return []
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise WorkflowFileError(f"{WORKFLOWS_FILENAME} YAML 解析失敗：{e}") from e
    if data is None:
        return []
    if not isinstance(data, dict) or not isinstance(data.get("workflows"), list):
        raise WorkflowFileError(f"{WORKFLOWS_FILENAME} 結構不符：頂層須為 `workflows:` 列表")
    out: list[dict] = []
    for i, item in enumerate(data["workflows"]):
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("name"), str)
            or not isinstance(item.get("stages"), list)
        ):
            raise WorkflowFileError(
                f"{WORKFLOWS_FILENAME} 第 {i + 1} 筆結構不符：須含 name(str)/stages(list)"
            )
        out.append(
            {
                "name": item["name"],
                "description": item.get("description", "") or "",
                "stages": item["stages"],
            }
        )
    return out


def _save_workflows(workflows: list[dict]) -> None:
    """全量落檔（temp＋rename 原子寫；目錄不存在自動建立）。"""
    path = workflows_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    text = yaml.safe_dump({"workflows": workflows}, allow_unicode=True, sort_keys=False)
    tmp = path.with_suffix(".yaml.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def get_workflow(name: str) -> dict | None:
    """依名稱查 workflow；不存在回 None。保留名（無同名檔案）回對應內建定義。"""
    name = (name or "").strip()
    for w in list_workflows():
        if w["name"] == name:
            return w
    if name in _BUILTIN_WORKFLOWS:
        return _BUILTIN_WORKFLOWS[name]()
    return None


def create_workflow(name: str, description: str, stages: list) -> dict | None:
    """驗證後新增 workflow 並落檔；驗證失敗 raise WorkflowError，同名/保留名已存在回 None（→409）。"""
    wf = validate_workflow(name, description, stages)
    workflows = list_workflows()
    if any(w["name"] == wf["name"] for w in workflows) or wf["name"] in RESERVED_NAMES:
        return None
    workflows.append(wf)
    _save_workflows(workflows)
    logger.info("workflow 已建立：%s（%d stages）", wf["name"], len(wf["stages"]))
    return wf


def update_workflow(name: str, description: str, stages: list) -> dict | None:
    """整筆替換同名 workflow（name 由路徑決定、不可改名）；不存在回 None（→404）。"""
    wf = validate_workflow(name, description, stages)
    workflows = list_workflows()
    for i, w in enumerate(workflows):
        if w["name"] == wf["name"]:
            workflows[i] = wf
            _save_workflows(workflows)
            logger.info("workflow 已更新：%s（%d stages）", name, len(wf["stages"]))
            return wf
    return None


def delete_workflow(name: str) -> bool:
    """刪除 workflow；不存在回 False（→404）。"""
    name = (name or "").strip()
    workflows = list_workflows()
    kept = [w for w in workflows if w["name"] != name]
    if len(kept) == len(workflows):
        return False
    _save_workflows(kept)
    logger.info("workflow 已刪除：%s", name)
    return True
