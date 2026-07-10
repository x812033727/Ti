"""AntigravityExpert：teardown 收屍洩漏修復 + 單次逾時降級為軟失敗（不暫停整場）。

實測根因：agy `--sandbox` 的工具子命令在 agy 主程序退出後仍存活、握著 stdout/stderr pipe
寫端 → AntigravityExpert 的 `async for proc.stdout` 永不 EOF，整輪卡到外層 AUTOPILOT_TASK_TIMEOUT
（3600s），且累積一堆 PPID=1 孤兒 agy。修法：所有 teardown 路徑先 `runner.reap_group(pgid)`
整組收屍釋放 pipe、再有上限 join；逾時／暫態不再升 ProviderUnavailable 暫停整個 autopilot，
改回本輪系統 note 軟失敗。
"""

from __future__ import annotations

import asyncio
import os

import pytest

from studio import config, providers, runner
from studio.roles import BY_KEY


def _collect():
    bucket: list = []

    async def broadcast(ev) -> None:
        bucket.append(ev)

    return bucket, broadcast


# --- runner.reap_group ---------------------------------------------------


@pytest.mark.asyncio
async def test_reap_group_kills_by_pgid_after_leader_reaped(tmp_path):
    """以記下的 pgid 收屍：即使直屬子程序已被 reap，殘留孫程序仍要被殺。"""
    proc = await asyncio.create_subprocess_exec(
        "/bin/sh",
        "-c",
        "sleep 30 >/dev/null 2>&1 & exit 0",  # 背景孫程序後 leader 立即退出
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True,
    )
    pgid = proc.pid
    await proc.wait()  # leader 結束並被 reap → getpgid(pid) 已失效

    runner.reap_group(pgid)
    # 收屍可能非同步生效，給 SIGKILL 一點時間。
    for _ in range(50):
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            break
        await asyncio.sleep(0.05)
    with pytest.raises(ProcessLookupError):
        os.killpg(pgid, 0)


def test_reap_group_missing_group_is_silent():
    """對已不存在的 group 收屍不應拋例外。"""
    runner.reap_group(2_000_000_000)  # 幾乎不可能存在的 pgid


# --- runner.wait_process_exit（輪詢 returncode，不靠 pipe-bound proc.wait）-------


@pytest.mark.asyncio
async def test_wait_process_exit_returns_empty_on_clean_exit():
    proc = await asyncio.create_subprocess_exec(
        "/bin/sh", "-c", "exit 0", stdout=asyncio.subprocess.DEVNULL
    )
    loop = asyncio.get_running_loop()
    reason = await asyncio.wait_for(
        runner.wait_process_exit(
            proc,
            idle_timeout=5.0,
            hard_timeout=5.0,
            last_activity=lambda: loop.time(),
            started_at=loop.time(),
            poll=0.02,
        ),
        timeout=3,
    )
    assert reason == ""
    assert proc.returncode == 0


@pytest.mark.asyncio
async def test_wait_process_exit_hard_timeout_reason():
    proc = await asyncio.create_subprocess_exec(
        "/bin/sh", "-c", "sleep 30", stdout=asyncio.subprocess.DEVNULL, start_new_session=True
    )
    loop = asyncio.get_running_loop()
    try:
        reason = await asyncio.wait_for(
            runner.wait_process_exit(
                proc,
                idle_timeout=0.0,  # 只測 hard
                hard_timeout=0.3,
                last_activity=lambda: loop.time(),
                started_at=loop.time(),
                poll=0.05,
            ),
            timeout=3,
        )
        assert reason == "總時長"
    finally:
        runner.reap_group(proc.pid)


@pytest.mark.asyncio
async def test_wait_process_exit_idle_timeout_reason():
    proc = await asyncio.create_subprocess_exec(
        "/bin/sh", "-c", "sleep 30", stdout=asyncio.subprocess.DEVNULL, start_new_session=True
    )
    loop = asyncio.get_running_loop()
    fixed = loop.time()  # last_activity 不前進 → idle 必先到
    try:
        reason = await asyncio.wait_for(
            runner.wait_process_exit(
                proc,
                idle_timeout=0.3,
                hard_timeout=30.0,
                last_activity=lambda: fixed,
                started_at=fixed,
                poll=0.05,
            ),
            timeout=3,
        )
        assert reason == "閒置"
    finally:
        runner.reap_group(proc.pid)


# --- _antigravity_pause_or_soft -----------------------------------------


@pytest.mark.parametrize(
    "detail,expected",
    [
        ("審查意見：本輪無阻擋項目。", None),  # 正常輸出
        ("Error: timed out waiting for response", "soft"),  # 暫態：逾時 → 軟失敗
        ("You are not signed in. Please sign in.", "pause"),  # 硬：未登入 → 暫停
        ("not signed in", "pause"),  # 硬：未登入 → 暫停
        ("rate limit exceeded, retry later", "soft"),  # 暫態：裸 CLI 限流 → 軟失敗
        ("Too Many Requests", "soft"),  # 暫態：整行 429 慣用語 → 軟失敗
        ("建議替 API 加上 rate limit 設計", None),  # 白樣本：討論限流不誤殺
    ],
)
def test_antigravity_pause_or_soft(detail, expected):
    assert providers._antigravity_pause_or_soft(detail) == expected


@pytest.mark.parametrize(
    "detail,expected",
    [
        ("rate limit exceeded", "soft"),  # 暫態：裸 CLI 限流 → 軟失敗，不 pause 整場
        ("正常審查輸出，順帶討論 rate limit 概念", None),  # 白樣本
    ],
)
def test_codex_pause_or_soft_rate_limit(detail, expected):
    assert providers._codex_pause_or_soft(detail) == expected


# --- AntigravityExpert 整合（假 agy 腳本，沙箱關閉直接執行）-----------------


@pytest.fixture
def _antigravity_env(monkeypatch):
    monkeypatch.setattr(config, "ANTIGRAVITY_SANDBOX", False)
    monkeypatch.setattr(config, "ANTIGRAVITY_SKIP_PERMISSIONS", False)
    monkeypatch.setattr(config, "ANTIGRAVITY_MODEL_LEAD", "")
    monkeypatch.setattr(config, "ANTIGRAVITY_MODEL_FAST", "")


def _write_fake_agy(tmp_path, body: str) -> str:
    p = tmp_path / "fake_agy.sh"
    p.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    p.chmod(0o755)
    return str(p)


@pytest.mark.asyncio
async def test_normal_exit_with_leaked_grandchild_reaped_not_hung(
    monkeypatch, tmp_path, _antigravity_env
):
    """agy 退出但背景孫程序握著 stdout pipe：reap 後須迅速回傳，而非卡在 async-for。"""
    # 放大 join 上限：唯一能讓它「快速」回傳的，就是 reap_group 收掉孫程序讓 pipe EOF。
    monkeypatch.setattr(providers, "_READER_JOIN_TIMEOUT", 30.0)
    monkeypatch.setattr(config, "TURN_IDLE_TIMEOUT", 30.0)
    monkeypatch.setattr(config, "TURN_HARD_TIMEOUT", 30.0)
    agy = _write_fake_agy(
        tmp_path,
        "printf '審查意見：本輪無阻擋項目。\\n'\nsleep 30 &\nexit 0\n",
    )
    monkeypatch.setattr(config, "ANTIGRAVITY_BIN", agy)

    exp = providers.AntigravityExpert(BY_KEY["security"], "sess", tmp_path)
    _, broadcast = _collect()

    # 若 reap 失效，會卡到 30s join 上限；timeout=5 證明確實是 reap 讓它秒回。
    text = await asyncio.wait_for(exp.speak("審查任務", broadcast), timeout=5)
    assert "本輪無阻擋項目" in text


@pytest.mark.asyncio
async def test_turn_timeout_soft_fails_without_pause(monkeypatch, tmp_path, _antigravity_env):
    """agy 整輪無輸出卡住：watchdog 逾時須 reap 收屍並回系統 note 軟失敗，不升 ProviderUnavailable。"""
    monkeypatch.setattr(config, "TURN_IDLE_TIMEOUT", 0.5)
    monkeypatch.setattr(config, "TURN_HARD_TIMEOUT", 0.0)
    agy = _write_fake_agy(tmp_path, "sleep 30\n")  # 無輸出、卡住
    monkeypatch.setattr(config, "ANTIGRAVITY_BIN", agy)

    exp = providers.AntigravityExpert(BY_KEY["security"], "sess", tmp_path)
    bucket, broadcast = _collect()

    result = await asyncio.wait_for(exp.speak("審查任務", broadcast), timeout=8)

    # 軟失敗：回系統 note（不含核可關鍵詞），而非拋 ProviderUnavailable 暫停整場。
    assert result.startswith("【系統】") and "逾時" in result
    assert not any(h in result.lower() for h in ("核可", "通過", "approve", "lgtm"))
    # 最後狀態回 idle（speak 的 finally 有廣播）。
    assert bucket[-1].payload["status"] == "idle"


@pytest.mark.asyncio
async def test_provider_unavailable_still_pauses_on_hard_signal(
    monkeypatch, tmp_path, _antigravity_env
):
    """硬不可用（未登入）仍須升 ProviderUnavailable，由 autopilot 暫停——軟失敗只給暫態。"""
    monkeypatch.setattr(config, "TURN_IDLE_TIMEOUT", 30.0)
    monkeypatch.setattr(config, "TURN_HARD_TIMEOUT", 30.0)
    agy = _write_fake_agy(tmp_path, "printf 'You are not signed in. Please sign in.\\n'\nexit 0\n")
    monkeypatch.setattr(config, "ANTIGRAVITY_BIN", agy)

    exp = providers.AntigravityExpert(BY_KEY["security"], "sess", tmp_path)
    _, broadcast = _collect()

    with pytest.raises(providers.ProviderUnavailable):
        await asyncio.wait_for(exp.speak("審查任務", broadcast), timeout=5)
