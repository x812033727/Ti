"""Claude 多訂閱帳號：列舉本機已存的憑證標籤檔、查目前在線、切換（換檔）。

走訂閱時，Claude SDK/CLI 的認證讀「線上憑證」``~/.claude/.credentials.json``（路徑由
``config.CLAUDE_CREDENTIALS_FILE`` 決定）。要在同一台機器並存多個帳號，做法是把每個帳號
登入一次後的憑證另存成「標籤檔」，切換時換檔即可：

  - ``.credentials.json``            線上（SDK/CLI 實際使用，由 HOME 決定位置）
  - ``.credentials.acct-<label>.json``  各帳號標籤檔（登入一次後備份；label 為 A/B…）
  - ``.credentials.active``          純文字，記錄目前在線是哪個 label

切換 = 把線上檔存回「當前 label」標籤檔（保住自動續期後的最新 token）→ 複製「目標 label」
標籤檔覆蓋線上 → 改寫 ``.active``。本模組只做檔案層；認證在 SDK 啟動時載入記憶體，故換檔後
須由呼叫端重啟服務才生效（重啟邏輯不在此，避免本模組有副作用、好單測）。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from . import config

# label 僅允許英數/底線/連字號，長度 1~32：既當檔名片段也回給前端，須防路徑穿越。
_LABEL_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
_PREFIX = ".credentials.acct-"
_SUFFIX = ".json"


def _dir() -> Path:
    """標籤檔與線上檔共用的目錄（線上憑證檔的所在目錄）。"""
    return config.CLAUDE_CREDENTIALS_FILE.parent


def _active_file() -> Path:
    return _dir() / ".credentials.active"


def _label_file(label: str) -> Path:
    return _dir() / f"{_PREFIX}{label}{_SUFFIX}"


def valid_label(label: str) -> bool:
    return bool(_LABEL_RE.match(label or ""))


def active_label() -> str | None:
    """目前在線的 label；.active 檔缺失或內容非法時回 None。"""
    try:
        v = _active_file().read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return v if valid_label(v) else None


def _subscription(path: Path) -> str | None:
    """讀標籤檔的 subscriptionType（如 max/pro）；讀不到回 None。不回傳任何 token。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    sub = (data.get("claudeAiOauth") or {}).get("subscriptionType")
    return sub if isinstance(sub, str) and sub else None


def list_accounts() -> list[dict]:
    """掃目錄下所有 .credentials.acct-*.json，回每帳號的非秘密中繼資料。

    每筆 ``{label, cred_file, subscription, active}``，依 label 排序。找不到任何標籤檔
    時回 ``[]``（呼叫端可退回單帳號顯示）。``cred_file`` 供 claude_usage 查該帳號額度。
    """
    active = active_label()
    out: list[dict] = []
    for p in sorted(_dir().glob(f"{_PREFIX}*{_SUFFIX}")):
        label = p.name[len(_PREFIX) : -len(_SUFFIX)]
        if not valid_label(label):
            continue
        out.append(
            {
                "label": label,
                "cred_file": str(p),
                "subscription": _subscription(p),
                "active": label == active,
            }
        )
    return out


def _save_live_to(label: str) -> None:
    """把線上憑證檔內容存到 ``label`` 標籤檔並收斂權限（chmod 600）。

    供 ``switch()``（切走前保住自動續期後的最新 token）與 ``sync_active_label()``
    （在線 label 長期不切換時回寫快照）共用。呼叫端須自行確認線上檔存在。
    """
    dest = _label_file(label)
    dest.write_bytes(config.CLAUDE_CREDENTIALS_FILE.read_bytes())
    try:
        dest.chmod(0o600)
    except OSError:
        pass


def _expires_at(path: Path) -> float | None:
    """讀憑證檔 ``claudeAiOauth.expiresAt``（毫秒 epoch）；缺檔/壞檔/非數值回 None。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    exp = (data.get("claudeAiOauth") or {}).get("expiresAt")
    return float(exp) if isinstance(exp, (int, float)) else None


def sync_active_label() -> bool:
    """線上憑證比在線 label 標籤檔新（expiresAt 較大）時，回寫標籤檔；有回寫回 True。

    線上檔由 Claude CLI/SDK 自動續期，但標籤檔只在 ``switch()`` 時回存——在線 label 長期
    不切換就 stale，額度查詢會因 expiresAt 過期短路回 unauthorized。呼叫端（如
    provider_quota.snapshot）在讀多帳號額度前先呼叫本函式即可保持在線 label 快照新鮮。
    任何條件不符（無在線 label、線上檔/標籤檔缺失、expiresAt 讀不到或未較新）皆回 False，
    不拋例外。
    """
    active = active_label()
    if not active:
        return False
    live = config.CLAUDE_CREDENTIALS_FILE
    label_file = _label_file(active)
    if not live.exists() or not label_file.exists():
        return False
    live_exp = _expires_at(live)
    label_exp = _expires_at(label_file)
    if live_exp is None or label_exp is None or live_exp <= label_exp:
        return False
    try:
        _save_live_to(active)
    except OSError:
        return False
    return True


def switch(label: str) -> None:
    """把線上憑證切到 ``label`` 對應的帳號。

    label 非法或標籤檔不存在時 raise ValueError。流程：先把線上檔（含自動續期後最新 token）
    存回「當前 label」標籤檔，避免下次切回時用到舊 token；再以目標標籤檔覆蓋線上、改寫 .active。
    純檔案操作，不重啟服務（呼叫端負責），故本身可在單測中安全執行。
    """
    if not valid_label(label):
        raise ValueError(f"非法帳號標籤: {label!r}")
    target = _label_file(label)
    if not target.exists():
        raise ValueError(f"找不到帳號 {label} 的憑證檔")

    live = config.CLAUDE_CREDENTIALS_FILE
    cur = active_label()
    # 1) 線上檔存回當前 label（保住自動續期後的最新 token；當前 label 未知/標籤檔不在則略過）
    if cur and live.exists():
        cur_file = _label_file(cur)
        if cur_file.exists() and cur_file != target:
            _save_live_to(cur)
    # 2) 目標標籤檔覆蓋線上，並收斂權限
    live.write_bytes(target.read_bytes())
    try:
        live.chmod(0o600)
    except OSError:
        pass
    # 3) 標記在線
    _active_file().write_text(label, encoding="utf-8")


def _load(windows: dict[str, float | None] | None) -> float | None:
    """帳號負載＝5h／7d 兩額度窗 used_percentage 取最大。

    None 的窗忽略（查不到該窗不影響另一窗）；兩窗皆 None 回 None（帳號額度完全查不到）。
    """
    if not windows:
        return None
    vals = [v for v in (windows.get("five_hour"), windows.get("seven_day")) if v is not None]
    return max(vals) if vals else None


def pick_account(
    usages: dict[str, dict[str, float | None]],
    active: str | None,
    preferred: str,
    threshold: float,
    margin: float,
) -> str | None:
    """雙（多）帳號的「負載平均分配」純決策：回「應切換到的 label」，不需切換回 None。

    ``usages`` 為 ``{label: {"five_hour": 用量%|None, "seven_day": 用量%|None}}``；每帳號
    **負載＝兩窗取最大**（None 窗忽略；兩窗皆 None＝額度查不到→該帳號不可用，不得作為
    切換目標；在線帳號兩窗皆 None 則視為需要切走）。``preferred`` 為主帳號（同分優先）。

    規則（平均分配為主、``threshold`` 為安全上限、``margin`` 為遲滯）：

    1. 候選＝負載 < ``threshold``（安全上限，預設 95%）的帳號；無候選 → None
       （全部達上限或查不到，交給既有 quota gate 睡到額度重置）。
    2. best＝候選中負載最低者；同分 tie-break：``preferred`` 優先、再字母序。
    3. 在線帳號不在候選（負載達安全上限、或額度查不到）→ 回 best（安全上限強制切）。
    4. 在線帳號在候選 → 只有當 best ≠ 在線且「在線負載 − best 負載 ≥ ``margin``」才回
       best（**平均分配主規則**：把用量攤平到各帳號；margin 遲滯避免兩帳號負載相近時
       頻繁互切、每次切換都要重啟服務）。差距未達 margin → None（留在原帳號）。

    在線 label 未知（``active is None``）一律回 None——不知道現在是誰就不動作，寧可
    不切也不要亂切。純函式、無 I/O，好單測。
    """
    if active is None:
        return None
    loads = {label: _load(windows) for label, windows in usages.items()}
    candidates = {label: ld for label, ld in loads.items() if ld is not None and ld < threshold}
    if not candidates:
        return None  # 全部達安全上限／查不到 → 交給 quota gate
    # 負載最低者；同分 preferred 優先（False < True）、再字母序
    best = min(candidates, key=lambda lb: (candidates[lb], lb != preferred, lb))
    active_load = candidates.get(active)
    if active_load is None:
        return best  # 在線達安全上限或不可用（best 必 ≠ active，因 active 不在候選）
    if best != active and active_load - candidates[best] >= margin:
        return best
    return None
