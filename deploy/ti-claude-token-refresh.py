#!/opt/ti/.venv/bin/python
"""Ti 多帳號 Claude 訂閱 OAuth token 保鮮(非在線帳號)。

背景:額度查詢(studio.claude_usage.fetch_rate_limits)只讀 accessToken、不做 OAuth 續期;
非在線帳號從不被 SDK 拿去跑討論,其 access token 過期後就永遠 unauthorized,被 provider_quota
映射成 stale_label → pick_account 視為不可用不得切入 → 輪替永凍在在線帳號(2026-07-06 死鎖)。
本腳本定期用各帳號的 refreshToken 原地換發新 access token,讓非在線帳號額度恆可見。

安全設計:
- **只刷非在線帳號**。在線帳號由 SDK 自己續期;外部續期會輪替掉 refreshToken(單次有效),
  可能弄壞 SDK 記憶體裡舊 refreshToken 的續期鏈。在線標籤讀 ~/.claude/.credentials.active。
- 只刷「快過期或已過期」者(REFRESH_IF_LEFT_H 內),避免每 tick 都無謂輪替 token。
- 原子寫回、保 0600、絕不印 token 值;單帳號失敗不影響其他帳號。
- 必須用 httpx(urllib 的 TLS 簽名會被 Cloudflare error 1010 擋)。
"""

from __future__ import annotations

import glob
import json
import os
import re
import sys
import time

import httpx

CREDS_DIR = "/root/.claude"
ACTIVE_FILE = os.path.join(CREDS_DIR, ".credentials.active")
ACCT_GLOB = os.path.join(CREDS_DIR, ".credentials.acct-*.json")
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
ENDPOINT = "https://console.anthropic.com/v1/oauth/token"
REFRESH_IF_LEFT_H = 3.0  # token 壽命 ~8h;剩 <3h(或已過期)才換,配合每時 tick 恆保有效
_LABEL_RE = re.compile(r"\.credentials\.acct-(.+)\.json$")


def _active_label() -> str | None:
    try:
        return open(ACTIVE_FILE, encoding="utf-8").read().strip() or None
    except OSError:
        return None


def _refresh(path: str, label: str) -> str:
    d = json.load(open(path, encoding="utf-8"))
    o = d.get("claudeAiOauth") or {}
    exp = o.get("expiresAt")
    rt = o.get("refreshToken")
    if not rt:
        return f"{label}: 無 refreshToken,略過(需重新登入)"
    left_h = (exp / 1000 - time.time()) / 3600 if isinstance(exp, (int, float)) else -999
    if left_h > REFRESH_IF_LEFT_H:
        return f"{label}: 尚有 {left_h:.1f}h,免刷"

    payload = {"grant_type": "refresh_token", "refresh_token": rt, "client_id": CLIENT_ID}
    r = httpx.post(ENDPOINT, json=payload, headers={"Content-Type": "application/json"}, timeout=30)
    if r.status_code != 200:
        return f"{label}: refresh 失敗 HTTP {r.status_code} {r.text[:120]}"
    res = r.json()
    at = res.get("access_token")
    ei = res.get("expires_in")
    if not at or not ei:
        return f"{label}: 回應缺欄位 {sorted(res.keys())}"
    o["accessToken"] = at
    o["refreshToken"] = res.get("refresh_token") or rt
    o["expiresAt"] = int(time.time() * 1000) + int(ei) * 1000
    d["claudeAiOauth"] = o
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return f"{label}: 已續期,新壽命 {int(ei) / 3600:.1f}h(was {left_h:.1f}h)"


def main() -> int:
    active = _active_label()
    files = sorted(glob.glob(ACCT_GLOB))
    if not files:
        print("ti-claude-token-refresh: 找不到帳號憑證檔,略過")
        return 0
    for path in files:
        m = _LABEL_RE.search(path)
        label = m.group(1) if m else os.path.basename(path)
        if label == active:
            print(f"{label}: 在線帳號,交給 SDK 自刷,略過")
            continue
        try:
            print("ti-claude-token-refresh:", _refresh(path, label))
        except Exception as e:  # noqa: BLE001 — 單帳號失敗不影響其他
            print(f"{label}: 例外 {type(e).__name__}: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
