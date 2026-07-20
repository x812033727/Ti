"""Claude 多訂閱帳號：列舉本機已存的憑證標籤檔、查目前在線、切換（換檔）。

走訂閱時，Claude SDK/CLI 的認證讀「線上憑證」``~/.claude/.credentials.json``（路徑由
``config.CLAUDE_CREDENTIALS_FILE`` 決定）。要在同一台機器並存多個帳號，做法是把每個帳號
登入一次後的憑證另存成「標籤檔」，切換時換檔即可：

  - ``.credentials.json``            線上（SDK/CLI 實際使用，由 HOME 決定位置）
  - ``.credentials.acct-<label>.json``  各帳號標籤檔（登入一次後備份；label 為 A/B…）
  - ``.credentials.active``          純文字，記錄目前在線是哪個 label
  - ``.credentials.pin``             純文字，使用者釘選的 label（手動模式；缺檔＝自動輪替）

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


def _pin_file() -> Path:
    return _dir() / ".credentials.pin"


def pinned_label() -> str | None:
    """使用者釘選（手動模式）的 label；pin 檔缺失或內容非法時回 None（＝自動模式）。

    pin 檔只存純文字 label（與 ``.credentials.active`` 同格式），刻意不觸碰任何憑證
    JSON——切換/回寫憑證檔曾發生跨帳號污染事故，pin 機制保持零污染面。
    """
    try:
        v = _pin_file().read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return v if valid_label(v) else None


def set_pinned(label: str | None) -> None:
    """寫入/清除釘選：``label=None`` 刪 pin 檔（回自動模式）；非法 label raise ValueError。"""
    if label is None:
        _pin_file().unlink(missing_ok=True)
        return
    if not valid_label(label):
        raise ValueError(f"非法帳號標籤: {label!r}")
    pin = _pin_file()
    pin.parent.mkdir(parents=True, exist_ok=True)
    pin.write_text(label, encoding="utf-8")


def label_exists(label: str) -> bool:
    """label 合法且對應的憑證標籤檔存在。供釘選/切換前驗證目標帳號可用。"""
    return valid_label(label) and _label_file(label).exists()


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

    每筆 ``{label, cred_file, subscription, active, pinned}``，依 label 排序。找不到任何
    標籤檔時回 ``[]``（呼叫端可退回單帳號顯示）。``cred_file`` 供 claude_usage 查該帳號
    額度；``pinned``＝使用者釘選（手動模式）目標。
    """
    active = active_label()
    pinned = pinned_label()
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
                "pinned": label == pinned,
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
    return float(exp) if isinstance(exp, int | float) else None


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


def _reset_of(windows: dict[str, float | None] | None, key: str = "five_hour_reset") -> float:
    """帳號額度窗的重置時間（epoch 秒）；``key`` 選窗（five_hour_reset／seven_day_reset），
    查不到（None）視為 +inf（最晚重置）。"""
    r = (windows or {}).get(key)
    return float(r) if isinstance(r, int | float) else float("inf")


def _earlier_reset_target(
    usages: dict[str, dict[str, float | None]],
    candidates: dict[str, float],
    key: str,
    edge: float,
) -> str | None:
    """「早重置多吃」單一窗的判定：候選中最早重置者比次早者早 ≥ ``edge`` 秒時回其 label；
    規則不成立（差距不足）或任一方重置未知回 None（交給下一層）。

    **兩者的重置時間都必須已知（finite）**才判定——v3 曾把未知視為 +inf 直接比較，
    ``inf - earliest >= edge`` 恆真，在線又恰是「唯一已知重置者」時會永遠回「留在線」、
    絕不下沉到負載平衡（黏著 bug）；資訊不完整時本層不該有意見。
    """
    by_reset = sorted(candidates, key=lambda lb: _reset_of(usages.get(lb), key))
    earliest, second = by_reset[0], by_reset[1]
    earliest_reset = _reset_of(usages.get(earliest), key)
    second_reset = _reset_of(usages.get(second), key)
    if earliest_reset == float("inf") or second_reset == float("inf"):
        return None
    return earliest if second_reset - earliest_reset >= edge else None


def scoped_used_pct(model: str, models_usage: dict | None) -> float | None:
    """某帳號對 ``model`` 的「按模型 scoped」週限用量%（如 Fable 的 weekly_scoped）；查不到回 None。

    ``models_usage`` = ``claude_usage.fetch_rate_limits()["models"]``，鍵為模型 display_name
    （如 ``"Fable"``）。比對規則：display_name（小寫）出現在 model id（小寫）內即命中（涵蓋
    ``fable`` → ``claude-fable-5`` 這類命名），回其 ``used_percentage``。此為 scoped 比對的
    SSOT，``experts._scoped_exhausted`` 亦委派本函式，避免兩處規則各自漂移。
    """
    if not model or not isinstance(models_usage, dict):
        return None
    mid = model.lower()
    for disp, w in models_usage.items():
        if not isinstance(w, dict):
            continue
        pct = w.get("used_percentage")
        if not isinstance(pct, int | float):
            continue
        if disp and str(disp).lower() in mid:
            return float(pct)
    return None


def _scoped_blocked(windows: dict[str, float | None] | None, threshold: float) -> bool:
    """該帳號的釘選模型 scoped 週限（如 Fable weekly_scoped）是否已達上限。

    scoped 未填（None）＝資訊不明，回 False（不排除該帳號，維持向後相容）。已爆的帳號
    做不了主要工作（experts 會把它改派成較貴備援空耗全域額度），故「早重置/負載」選擇
    應把它排除，才不會與 scoped 救援層互相拉扯來回 flap。
    """
    s = (windows or {}).get("scoped")
    return isinstance(s, int | float) and s >= threshold


def pick_account(
    usages: dict[str, dict[str, float | None]],
    active: str | None,
    preferred: str,
    threshold: float,
    margin: float,
    reset_edge: float,
    reset_edge_7d: float,
    scoped_threshold: float = 95.0,
) -> str | None:
    """雙（多）帳號分配的純決策（v4）：回「應切換到的 label」，不需切換回 None。

    ``usages`` 為 ``{label: {"five_hour": %|None, "seven_day": %|None,
    "five_hour_reset": epoch|None, "seven_day_reset": epoch|None}}``；每帳號**負載＝
    5h/7d 兩用量窗取最大**（None 窗忽略；兩窗皆 None＝額度查不到→不可用，不得作為
    切換目標；在線帳號不可用則視為需要切走）。優先序：**安全上限 > scoped 週限救援 >
    7d 早重置 > 5h 早重置 > 負載平衡**：

    1. 安全上限：候選＝負載 < ``threshold``（預設 95%）的帳號——負載達上限者即使最早
       重置也**不得**為切換目標；無候選 → None（交給既有 quota gate 睡到額度重置）；
       在線不在候選（達上限或不可用）→ 強制切到下述 target（不受 margin 遲滯限制）。
    1.5 scoped 週限救援：在線帳號的 ``usages[active]["scoped"]``（如 PM 釘的 Fable 之
        weekly_scoped 用量%，由呼叫端以 ``scoped_used_pct`` 填入；未填＝None＝本層略過）
        ≥ ``scoped_threshold`` 而某候選 scoped < ``scoped_threshold`` → target＝該候選。
        補全域閘門盲點：provider_quota 只看全域 5h/7d、刻意不含 scoped，故在線 Fable 撞週限
        時釘 Fable 的專家全被 ``experts`` 改派成較貴備援（opus）空耗全域額度；有另一帳號
        Fable 仍新鮮時切過去讓 Fable 恢復可用更省。候選已排除全域達上限者（不會切到爆帳號）；
        在線 scoped 未知/未滿 → 不介入。切 scoped 最低者（同分 preferred 優先、再字母序）；
        換帳號本身要重啟即遲滯,不另設 margin。**防回切靠下述 pool 過濾**：救援後新在線
        scoped 低雖使本層下輪不再觸發,但若不把 scoped 已爆的舊帳號逐出「早重置/負載」
        候選,2a/2b 會因它全域較早重置而把在線拉回去→又觸發本層→無止盡 flap（每切一次
        一次重啟）。故第 2、3 層改用 **scoped-unblocked 池**（``pool``）：``_scoped_blocked``
        （scoped ≥ 門檻）者剔除,scoped 全 None 時 pool==candidates 完全相容,全爆時退回
        candidates。
    2a. 7d 早重置多吃：**池內**（pool，非全候選）``seven_day_reset`` **兩者皆已知**且最早者比次早者早
        ≥ ``reset_edge_7d``（秒）→ target＝最早重置者。7d 窗優先於 5h 窗：7d 是
        「週尺度的稀缺資源」，早歸還的額度不先吃掉就是白白浪費一整週的配額；5h 窗
        每天循環多次、只是節奏問題，錯過下一輪就補回來。
    2b. 5h 早重置多吃：同規則、比 ``five_hour_reset``、門檻 ``reset_edge``（秒）。
        **兩者重置皆已知才判定**（v3 把未知當 +inf，單邊已知時 ``inf ≥ edge`` 恆真，
        會卡死「留在線」永不下沉——黏著 bug，已修）；否則下沉到負載平衡。
        2a/2b 的 edge 差距本身就是遲滯：由重置規則選出且 target ≠ 在線即切；
        target＝在線 → None（留在線多吃，**不**再下沉）。
    3. 負載平衡（同 v2）：重置規則皆不成立時退回——target＝負載最低者（同分
       tie-break：``preferred`` 優先、再字母序）；在線在候選時須「在線負載 −
       target 負載 ≥ ``margin``」（遲滯，避免頻繁互切重啟）才切，否則 None。

    在線 label 未知（``active is None``）一律回 None——不知道現在是誰就不動作，寧可
    不切也不要亂切。純函式、無 I/O，好單測。
    """
    if active is None:
        return None
    loads = {label: _load(windows) for label, windows in usages.items()}
    candidates = {label: ld for label, ld in loads.items() if ld is not None and ld < threshold}
    if not candidates:
        return None  # 全部達安全上限／查不到 → 交給 quota gate
    # 第 1.5 層：scoped 週限（如 PM 釘的 Fable）救援——在線 scoped 已滿但某候選 scoped 仍有餘
    # → 切去該候選。候選已排除全域達上限者；在線 scoped 未知/未滿則本層不介入,下沉既有規則。
    on_scoped = (usages.get(active) or {}).get("scoped")
    if isinstance(on_scoped, int | float) and on_scoped >= scoped_threshold:
        relief = [
            lb
            for lb in candidates
            if lb != active
            and isinstance((usages.get(lb) or {}).get("scoped"), int | float)
            and (usages.get(lb) or {})["scoped"] < scoped_threshold
        ]
        if relief:
            return min(relief, key=lambda lb: (usages[lb]["scoped"], lb != preferred, lb))
    # scoped-unblocked 池：釘選模型 scoped 已爆的帳號無法做主要工作（回去只會被 experts
    # 改派較貴備援空耗全域額度），故下面「早重置/負載」選擇不得挑中它們——否則會與第 1.5
    # 層 scoped 救援永無止盡地互切 flap（在線 A：scoped 爆→救援→切 B；到 B：早重置規則
    # 見 A 全域 5h/7d 皆較早重置→拉回 A；A scoped 仍爆→又救援→B…每切一次一次重啟）。
    # scoped 全 None（未填/純負載模式）時 scoped_ok == candidates，完全向後相容；全部
    # scoped 皆爆時退回 candidates（都做不了主要工作，至少讓全域早重置/負載規則維持運作）。
    scoped_ok = {
        lb: ld
        for lb, ld in candidates.items()
        if not _scoped_blocked(usages.get(lb), scoped_threshold)
    }
    pool = scoped_ok or candidates
    # 第 2 層：早重置多吃——7d 窗（稀缺資源）優先於 5h 窗（節奏）；規則成立即定案。
    if len(pool) >= 2:
        for key, edge in (("seven_day_reset", reset_edge_7d), ("five_hour_reset", reset_edge)):
            target = _earlier_reset_target(usages, pool, key, edge)
            if target is not None:
                # 非在線就切（含在線不在候選的強制切）；在線即最早者 → 留著多吃
                return target if target != active else None
    # 第 3 層：負載平衡（同 v2）——負載最低者；同分 preferred 優先（False < True）、再字母序
    best = min(pool, key=lambda lb: (pool[lb], lb != preferred, lb))
    active_load = pool.get(active)
    if active_load is None:
        return best  # 在線達安全上限/不可用/scoped 爆（best 必 ≠ active，因 active 不在池）
    if best != active and active_load - pool[best] >= margin:
        return best
    return None
