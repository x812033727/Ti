"""角色設定檔載入器：`roles/*.md`（Markdown＋YAML frontmatter，一檔一角色）。

檔案格式
--------
- 檔名即角色 key（`<key>.md`，須符合 ``^[a-z][a-z0-9_]{1,31}$``）；frontmatter 寫了
  ``key`` 則必須與檔名一致。
- YAML frontmatter 欄位（pydantic 驗證、``extra="forbid"``——未知欄位明確報錯）：
    key: 選填，須等於檔名 stem
    name: 必填，中文顯示名
    avatar: 選填，emoji（預設 "🤖"）
    title: 選填，職稱（預設同 key）
    model: 選填（預設 config.MODEL_FAST）
    allowed_tools: 選填 list[str]（預設 ["Read", "Grep"]）
    permission_mode: 選填，白名單 {default, acceptEdits}（預設 "default"）
    tags: 選填 list[str]
    description: 選填，給調度／選人看的一句話描述
- body 即「角色專屬 system prompt」；載入時自動前置 roles._COMMON（共通守則），
  故檔案不必（也不應）手抄共通段。
- 反空殼 persona（micro-rules）：body 去空白後須非空，且至少一行含
  「輸出/決議/驗證/格式/指令/決策」緊接冒號（半形或全形）——確保角色有可解析的出力格式。

載入規則
--------
- 內建 8 角色為預設；檔案同 key 覆蓋內建，新 key 追加。
- ``BY_KEY`` ＝ 全部內建（含被 config.OPTIONAL_ROLES 過濾者）＋全部合法檔案角色
  （維持 BY_KEY ⊇ ROSTER 的既有不對稱——improver 靠 ``key not in BY_KEY`` 判斷）。
- ``ROSTER`` ＝ 內建（同 key 檔案覆蓋後、沿用 OPTIONAL_ROLES 過濾）＋全部新 key
  檔案角色（依 key 排序，確保順序確定性）。
- 壞檔逐檔拒絕並 log 原因（logger ``ti.roles``），不影響其他檔與內建角色。
- ``reload_roles()`` 為純同步函式：先完整 build 新資料，再一次原地變異
  ``ROSTER[:]`` / ``BY_KEY.clear()+update()`` / ``CORE_ROLES[:]`` / 具名常數
  setattr——既有模組級 import 綁定保活、無並發空窗。
- reload 語意：進行中 session 已快照 Role 物件，reload 只影響之後建立的 expert。

主要介面
--------
- ``reload_roles() -> dict[str, str]``：掃 config.ROLES_DIR 合併進 roles 模組，
  回傳 {key: 錯誤訊息} 的壞檔清單（空 dict＝全部成功）。
- ``parse_role_file(path) -> Role``：解析單一角色檔（壞檔 raise RoleFileError）。
- ``validate_persona_body(body) -> None``：反空殼 persona 驗證（API 層共用）。
- ``role_source(key) -> str``：'builtin' | 'override' | 'file' | 'unknown'。
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from . import config, roles
from .roles import Role

logger = logging.getLogger("ti.roles")

# 角色 key 格式（檔名 stem 與 API 路徑參數同一套，防路徑穿越／怪字元）。
KEY_RE = re.compile(r"^[a-z][a-z0-9_]{1,31}$")

# 反空殼 persona：body 至少一行需含「出力格式」關鍵詞＋冒號。
# 內建 8 角色 body 全數匹配（tests/core/test_role_store.py 有守門單測），
# 確保 override 內建角色的「讀出→改→寫回」往返不會被本規則卡死。
_PERSONA_RE = re.compile(r"(輸出|決議|驗證|格式|指令|決策)[:：]")

# permission_mode 白名單（對齊內建角色實際使用的兩種）。
PERMISSION_MODES = ("default", "acceptEdits")

# frontmatter 分割：--- ... --- 之後全部是 body。
_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?(.*)\Z", re.S)

# 內建角色 key → roles 模組具名常數名（improver/autopilot 以函式內 import 取用，
# 覆蓋內建時必須同步 setattr，否則同一角色出現兩種行為）。
_BUILTIN_CONST = {
    "pm": "PM",
    "engineer": "ENGINEER",
    "qa": "QA",
    "senior": "SENIOR",
    "researcher": "RESEARCHER",
    "architect": "ARCHITECT",
    "security": "SECURITY",
    "devops": "DEVOPS",
}

# 最近一次 reload 載入成功的「檔案角色」key 集合（role_source 用）。
_file_keys: set[str] = set()


class RoleFileError(ValueError):
    """角色檔不合法（格式/欄位/persona 驗證失敗），訊息為人讀原因。"""


class RoleFileModel(BaseModel):
    """frontmatter 的 pydantic 驗證模型。未知欄位明確報錯（extra='forbid'）。"""

    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    key: str | None = None
    name: str
    avatar: str = "🤖"
    title: str = ""
    model: str = ""
    allowed_tools: list[str] = Field(default_factory=lambda: ["Read", "Grep"])
    permission_mode: str = "default"
    tags: list[str] = Field(default_factory=list)
    description: str = ""

    @field_validator("name")
    @classmethod
    def _name_nonempty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("name 不可為空")
        return v

    @field_validator("permission_mode")
    @classmethod
    def _permission_whitelist(cls, v: str) -> str:
        if v not in PERMISSION_MODES:
            raise ValueError(f"permission_mode 須為 {PERMISSION_MODES} 之一，收到 {v!r}")
        return v

    @field_validator("allowed_tools", "tags")
    @classmethod
    def _items_nonempty(cls, v: list[str]) -> list[str]:
        out = [s.strip() for s in v]
        if any(not s for s in out):
            raise ValueError("清單項目不可為空字串")
        return out


def validate_persona_body(body: str) -> None:
    """反空殼 persona 驗證：對「角色專屬 body 原文」（前置 _COMMON 之前）檢查。

    不符即 raise RoleFileError，訊息指明缺什麼。API 層（/api/roles）共用本函式。
    """
    text = body.strip()
    if not text:
        raise RoleFileError("system_prompt（body）不可為空")
    if not _PERSONA_RE.search(text):
        raise RoleFileError(
            "body 缺出力格式段落（micro-rules）：至少一行需含"
            "「輸出/決議/驗證/格式/指令/決策」緊接冒號，"
            "例如「最後一行輸出：`決議: 核可` 或 `決議: 退回`」——拒絕只有形容詞的空殼 persona"
        )


def builtin_body(role: Role) -> str:
    """內建角色「去除 _COMMON 前綴」的專屬 body（override 編輯往返與守門單測用）。"""
    return role.system_prompt.removeprefix(roles._COMMON)


def parse_role_file(path: Path) -> Role:
    """解析並驗證單一角色檔，回傳 frozen Role；任何不合法 raise RoleFileError。"""
    key = path.stem
    if not KEY_RE.match(key):
        raise RoleFileError(f"檔名 {path.name!r} 不是合法角色 key（須符合 {KEY_RE.pattern}）")

    text = path.read_text(encoding="utf-8")
    m = _FRONTMATTER_RE.match(text)
    if not m:
        raise RoleFileError("缺 YAML frontmatter（檔案須以 '---' 起、再以 '---' 行收）")
    fm_text, body = m.group(1), m.group(2)

    try:
        data = yaml.safe_load(fm_text)
    except yaml.YAMLError as e:
        raise RoleFileError(f"frontmatter YAML 解析失敗：{e}") from e
    if not isinstance(data, dict):
        raise RoleFileError("frontmatter 須為 YAML 映射（key: value）")

    try:
        fm = RoleFileModel.model_validate(data)
    except ValidationError as e:
        # pydantic 訊息冗長，壓成單行人讀原因（含未知欄位/缺必填欄位的明確指名）。
        reasons = "; ".join(
            f"{'.'.join(str(p) for p in err['loc']) or '<root>'}: {err['msg']}"
            for err in e.errors()
        )
        raise RoleFileError(f"frontmatter 欄位驗證失敗：{reasons}") from e

    if fm.key is not None and fm.key != key:
        raise RoleFileError(f"frontmatter key={fm.key!r} 與檔名 {key!r} 不一致")

    validate_persona_body(body)

    return Role(
        key=key,
        name=fm.name,
        avatar=fm.avatar,
        title=fm.title or key,
        model=fm.model or config.MODEL_FAST,
        allowed_tools=list(fm.allowed_tools),
        permission_mode=fm.permission_mode,
        system_prompt=roles._COMMON + "\n" + body.strip(),
        tags=list(fm.tags),
        description=fm.description,
    )


def load_file_roles(roles_dir: Path) -> tuple[dict[str, Role], dict[str, str]]:
    """掃描目錄載入全部合法角色檔。

    回傳 (合法角色 {key: Role}, 壞檔 {檔名: 原因})。壞檔逐檔拒絕並 log，
    絕不影響其他檔案與內建角色。只掃 ``*.md``（範例檔用 .sample 副檔名即不被載入）。
    """
    loaded: dict[str, Role] = {}
    errors: dict[str, str] = {}
    if not roles_dir.is_dir():
        return loaded, errors
    for path in sorted(roles_dir.glob("*.md")):
        try:
            role = parse_role_file(path)
        except (RoleFileError, OSError, UnicodeDecodeError) as e:
            errors[path.name] = str(e)
            logger.warning("角色檔 %s 被拒絕：%s", path, e)
            continue
        loaded[role.key] = role
    return loaded, errors


def role_source(key: str) -> str:
    """角色來源標記：builtin（純內建）/ override（檔案覆蓋內建）/ file（純檔案）/ unknown。"""
    builtin = key in _BUILTIN_CONST
    from_file = key in _file_keys
    if builtin and from_file:
        return "override"
    if builtin:
        return "builtin"
    if from_file:
        return "file"
    return "unknown"


def reload_roles() -> dict[str, str]:
    """以「內建為預設、檔案同 key 覆蓋」重建角色表，原地變異進 roles 模組。

    純同步：先完整 build 好新資料，再於無 await 的區塊一次變異——
    ``CORE_ROLES[:]`` / ``_OPTIONAL_ROLES[:]`` / ``ROSTER[:]`` /
    ``BY_KEY.clear()+update()``，並對全部內建 key setattr 具名常數
    （PM/ENGINEER/...），保住既有模組級綁定與函式內 import 的一致性。
    回傳壞檔 {檔名: 原因}（空 dict＝全部成功）。
    """
    file_roles, errors = load_file_roles(Path(config.ROLES_DIR))

    builtin = {r.key: r for r in roles.BUILTIN_ROLES}
    merged = dict(builtin)
    merged.update(file_roles)  # 同 key 檔案勝

    core = [merged[r.key] for r in roles.BUILTIN_CORE]
    optional = [merged[r.key] for r in roles.BUILTIN_OPTIONAL]
    roster = core + [r for r in optional if r.key in config.OPTIONAL_ROLES]
    roster += [file_roles[k] for k in sorted(file_roles) if k not in builtin]

    # --- 同步一次原地變異（無 await，杜絕並發讀到中間態）---------------------
    roles.CORE_ROLES[:] = core
    roles._OPTIONAL_ROLES[:] = optional
    roles.ROSTER[:] = roster
    roles.BY_KEY.clear()
    roles.BY_KEY.update(merged)
    for key, const in _BUILTIN_CONST.items():
        setattr(roles, const, merged[key])
    _file_keys.clear()
    _file_keys.update(file_roles)

    if file_roles:
        logger.info(
            "角色檔載入完成：%d 個（覆蓋內建 %s、新增 %s）",
            len(file_roles),
            sorted(k for k in file_roles if k in builtin) or "無",
            sorted(k for k in file_roles if k not in builtin) or "無",
        )
    return errors
