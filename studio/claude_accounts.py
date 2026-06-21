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
            cur_file.write_bytes(live.read_bytes())
    # 2) 目標標籤檔覆蓋線上，並收斂權限
    live.write_bytes(target.read_bytes())
    try:
        live.chmod(0o600)
    except OSError:
        pass
    # 3) 標記在線
    _active_file().write_text(label, encoding="utf-8")
