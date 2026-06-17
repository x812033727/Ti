"""早夭偵測守護測試：`deploy.health_check` 在服務已被 systemd 標記死亡時應提前 return，
不耗滿 `attempts × delay`（預設 ≈36s）。沿用 `runner.run_http_demo` 的「server 進程
退出即停等」原則（`proc.returncode is not None → break`），落到 systemctl 服務上。

測試守護範圍（4 條 case）：
- is-active=failed → 早退；`_run` 呼叫次數 < attempts、總耗時遠短於 attempts×delay
- is-active=activating + curl 永遠非 200 → 跑滿 attempts（反向黑樣本：釘住「activating
  不被誤判為死」；移除早夭判定本測試亦綠，但「activating 不該早退」語意仍受守）
- is-active=active + curl=200 → 通過、不跑沙箱缺套件檢查（沙箱齊）
- is-active=active + curl=200 + 沙箱缺套件 → False 含「沙箱依賴缺失」

非測試守護範圍（屬設計取捨，不寫進測試）：
- systemd 環境探測 fail-open（生產環境分支、無法 mock 進單元測試；測試統一 monkeypatch
  `shutil.which` 開啟「有 systemd」路徑以進入早夭分流）
- curl 內容正確性（既有 `attempts × delay` 逾時邏輯兜底）
- `_reinstall_and_restart` 的 systemctl restart 呼叫（屬安裝路徑，獨立函式）
- `rollback()` 對 `health_check` 的呼叫鏈（與 `redeploy()` 共用同一 `health_check` 函式，
  守前者即守後者；額外測試是 YAGNI）
"""

from __future__ import annotations

import time

import pytest

from studio import deploy


# --- helpers ------------------------------------------------------------


def _enable_systemctl(monkeypatch) -> None:
    """把 `shutil.which("systemctl")` 釘成有回傳，讓 health_check 進入 is-active 早夭分流。

    部分容器雖有 `/usr/bin/systemctl` 二進位但無 D-Bus，真實呼叫會失敗；測試統一以
    monkeypatch 把環境探測結果鎖成「有 systemd」，再用 `deploy._run` mock 控制 is-active
    的回傳——避免依賴測試機器的真實 systemd 狀態。
    """
    monkeypatch.setattr(
        deploy.shutil,
        "which",
        lambda cmd: "/bin/systemctl" if cmd == "systemctl" else None,
    )


def _disable_systemctl(monkeypatch) -> None:
    """把 `shutil.which("systemctl")` 釘成 None，模擬無 systemd 環境（fail-open 路徑）。"""
    monkeypatch.setattr(deploy.shutil, "which", lambda cmd: None)


# --- a) 早退正樣：is-active=failed → 早退 ------------------------------


async def test_isactive_failed_triggers_early_exit(monkeypatch):
    _enable_systemctl(monkeypatch)

    calls = {"n": 0}

    async def fake_run(cmd, cwd=None, timeout=600):
        calls["n"] += 1
        if cmd[:2] == ["systemctl", "is-active"]:
            # is-active 對 failed 服務回 rc=3、stdout="failed\n"
            return 3, "failed\n"
        return 0, "000"  # curl 不通

    monkeypatch.setattr(deploy, "_run", fake_run)

    # delay=0.5、attempts=20：若沒早退，總耗時 ≥ 9 × 0.5 = 4.5s；早退則 ≈ 0s。
    t0 = time.monotonic()
    ok, msg = await deploy.health_check(attempts=20, delay=0.5)
    elapsed = time.monotonic() - t0

    assert ok is False
    assert "退出" in msg and "is-active=failed" in msg
    # 雙斷言：呼叫次數 < attempts（早退沒走滿迴圈）＋ 耗時 << attempts × delay
    assert calls["n"] < 20, f"早退失敗：_run 被呼叫 {calls['n']} 次（< 20 為預期）"
    assert elapsed < 1.0, f"早退失敗：耗時 {elapsed:.2f}s（< 1.0s 為預期）"


async def test_isactive_inactive_and_unknown_also_early_exit(monkeypatch):
    """inactive / unknown 與 stdout 空（查詢失敗）同樣走早退——確認分流非特例處理。"""
    for state, ret in (("inactive", (3, "inactive\n")), ("unknown", (4, "unknown\n")), ("", (4, ""))):
        _enable_systemctl(monkeypatch)

        async def fake_run(cmd, cwd=None, timeout=600, _ret=ret):
            if cmd[:2] == ["systemctl", "is-active"]:
                return _ret
            return 0, "000"

        monkeypatch.setattr(deploy, "_run", fake_run)
        ok, msg = await deploy.health_check(attempts=10, delay=0)
        assert ok is False
        # 訊息以 `is-active=` 後接該狀態呈現；空 stdout 走 'unknown' fallback
        expected = state or "unknown"
        assert f"is-active={expected}" in msg, f"state={state!r} 的早退訊息未帶正確 is-active 標記"
        assert "退出" in msg


# --- b) 反向黑樣本：is-active=activating + curl 不通 → 跑滿 attempts -------


async def test_isactive_activating_does_not_early_exit(monkeypatch):
    """反向黑樣本：服務 activating（systemd 啟動中）不該被早退，跑滿 attempts。

    守「activating 不被誤判為死」語意——把 activating 放進早退分流是這次要修的對立面。
    """
    _enable_systemctl(monkeypatch)

    isactive_calls = 0
    curl_calls = 0

    async def fake_run(cmd, cwd=None, timeout=600):
        nonlocal isactive_calls, curl_calls
        if cmd[:2] == ["systemctl", "is-active"]:
            isactive_calls += 1
            return 0, "activating\n"
        curl_calls += 1
        return 0, "000"  # curl 永遠非 200

    monkeypatch.setattr(deploy, "_run", fake_run)

    ok, msg = await deploy.health_check(attempts=4, delay=0)

    assert ok is False
    # 跑滿 attempts：每輪 is-active + curl 各一次
    assert isactive_calls == 4
    assert curl_calls == 4
    # 訊息走原 attempts 邏輯（非早退訊息）
    assert "未回 200" in msg
    assert "is-active=" not in msg


# --- c) 健康路徑：is-active=active + curl=200 → 通過 --------------------


async def test_isactive_active_and_200_passes(monkeypatch):
    _enable_systemctl(monkeypatch)

    async def fake_run(cmd, cwd=None, timeout=600):
        if cmd[:2] == ["systemctl", "is-active"]:
            return 0, "active\n"
        return 0, "200"

    monkeypatch.setattr(deploy, "_run", fake_run)
    monkeypatch.setattr(deploy.config, "sandbox_missing_deps", lambda: [])

    ok, msg = await deploy.health_check(attempts=4, delay=0)
    assert ok is True
    assert "200" in msg and "通過" in msg


# --- d) 沙箱缺套件：is-active=active + 200 + 沙箱缺 → False ---------------


async def test_isactive_active_200_but_sandbox_missing(monkeypatch):
    _enable_systemctl(monkeypatch)

    async def fake_run(cmd, cwd=None, timeout=600):
        if cmd[:2] == ["systemctl", "is-active"]:
            return 0, "active\n"
        return 0, "200"

    monkeypatch.setattr(deploy, "_run", fake_run)
    monkeypatch.setattr(deploy.config, "sandbox_missing_deps", lambda: ["bwrap"])

    ok, msg = await deploy.health_check(attempts=4, delay=0)
    assert ok is False
    assert "沙箱依賴缺失" in msg and "bwrap" in msg


# --- 額外：fail-open 路徑（無 systemd → 略過早夭判定，跑原 attempts 邏輯）


async def test_no_systemctl_skips_isactive_check(monkeypatch):
    """fail-open：環境無 systemctl 時早夭判定整段略過，跑原 attempts 邏輯（語意不變）。"""
    _disable_systemctl(monkeypatch)

    isactive_calls = 0

    async def fake_run(cmd, cwd=None, timeout=600):
        nonlocal isactive_calls
        if cmd[:2] == ["systemctl", "is-active"]:
            isactive_calls += 1
            return 0, "failed\n"  # 縱使回 failed 也該被略過
        return 0, "000"

    monkeypatch.setattr(deploy, "_run", fake_run)
    monkeypatch.setattr(deploy.config, "sandbox_missing_deps", lambda: [])

    ok, msg = await deploy.health_check(attempts=3, delay=0)
    assert ok is False
    assert isactive_calls == 0  # 早夭判定被略過
    assert "未回 200" in msg  # 訊息走原 attempts 邏輯
