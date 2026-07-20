"""GitHub repo identity 正規化（host-aware）——發佈／自改安全護欄的單一真相。

由 autopilot 的 `_repo_key` 抽出成公用模組：bare `owner/repo`、GitHub HTTPS、GitHub SSH
視為同一 repo；**同 path 的非 GitHub host 一律視為不同**（回空字串），防「偽造同 path host」
繞過 repo 比對。owner allowlist 護欄（publisher.assert_repo_allowed）與 autopilot 的
repo 污染防護共用此處語意，兩者不可分歧。
"""

from __future__ import annotations

from urllib.parse import urlparse


def repo_key(value: str) -> str:
    """把 GitHub repo 位置壓成不分大小寫的 github.com/owner/repo identity。

    無法解析（格式不符／非 GitHub host）一律回空字串（fail-closed，由 caller 拒絕）。
    """
    raw = (value or "").strip()
    if not raw:
        return ""
    host = "github.com"
    if "://" in raw:
        parsed = urlparse(raw)
        if (parsed.hostname or "").lower() != "github.com":
            return ""
        raw = parsed.path
    elif "@" in raw and ":" in raw:
        remote_host, raw = raw.rsplit(":", 1)
        if (remote_host.rsplit("@", 1)[-1] or "").lower() != "github.com":
            return ""
    elif raw.startswith("github.com/"):
        raw = raw[len("github.com/") :]
    raw = raw.strip("/")
    if raw.endswith(".git"):
        raw = raw[:-4]
    parts = [part for part in raw.split("/") if part]
    if len(parts) == 2:
        return f"{host}/{'/'.join(parts)}".lower()
    return ""


def repo_owner(value: str) -> str:
    """回傳 repo 位置的 owner（小寫）；無法解析（非 GitHub host／格式不符）回空字串。

    與 repo_key 同一套解析語意（單一真相），空字串代表 fail-closed：caller 應拒絕。
    """
    key = repo_key(value)
    return key.split("/")[1] if key else ""
