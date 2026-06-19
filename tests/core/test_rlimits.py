"""子進程資源上限（studio/runner._rlimit_preexec + run_command）測試。

POSIX 專有（pytest.importorskip("resource")）；tests/core 不在 CI sandbox-test job 的固定選集，
且全程 sandbox=False（不需 bwrap），可在一般 test job（TI_SANDBOX=0）穩定執行。
"""

from __future__ import annotations

import inspect

import pytest

from studio import config, runner

pytest.importorskip("resource")


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setattr(config, "RLIMITS_ENABLED", True)
    monkeypatch.setattr(config, "SANDBOX_ENABLED", False)


def test_preexec_none_when_disabled(monkeypatch):
    monkeypatch.setattr(config, "RLIMITS_ENABLED", False)
    assert runner._rlimit_preexec() is None


def test_preexec_none_when_all_zero(monkeypatch):
    monkeypatch.setattr(config, "RLIMITS_ENABLED", True)
    monkeypatch.setattr(config, "RLIMIT_MEM_MB", 0)
    monkeypatch.setattr(config, "RLIMIT_CPU_S", 0)
    monkeypatch.setattr(config, "RLIMIT_FSIZE_MB", 0)
    assert runner._rlimit_preexec() is None


def test_preexec_callable_when_enabled(enabled):
    assert callable(runner._rlimit_preexec())


async def test_as_limit_applied_to_child(tmp_path, enabled, monkeypatch):
    """子進程真的看到我們設的 RLIMIT_AS（證明經 fork-exec 繼承生效）。"""
    monkeypatch.setattr(config, "RLIMIT_MEM_MB", 512)
    monkeypatch.setattr(config, "RLIMIT_CPU_S", 0)
    monkeypatch.setattr(config, "RLIMIT_FSIZE_MB", 0)
    code = "import resource;print(resource.getrlimit(resource.RLIMIT_AS)[0])"
    r = await runner.run_command(tmp_path, f"python3 -c {code!r} 2>&1", sandbox=False)
    assert r.ok, r.output
    assert r.output.strip() == str(512 * 1024 * 1024)


async def test_mem_limit_blocks_huge_alloc(tmp_path, enabled, monkeypatch):
    monkeypatch.setattr(config, "RLIMIT_MEM_MB", 512)  # 夠 Python 啟動、但擋 3GB 配置
    monkeypatch.setattr(config, "RLIMIT_CPU_S", 0)
    monkeypatch.setattr(config, "RLIMIT_FSIZE_MB", 0)
    r = await runner.run_command(
        tmp_path, 'python3 -c "bytearray(3*1024*1024*1024)" 2>&1', sandbox=False
    )
    assert not r.ok  # MemoryError → 非 0 退出


async def test_fsize_limit_enforced_and_toggle(tmp_path, enabled, monkeypatch):
    monkeypatch.setattr(config, "RLIMIT_FSIZE_MB", 1)
    monkeypatch.setattr(config, "RLIMIT_MEM_MB", 0)
    monkeypatch.setattr(config, "RLIMIT_CPU_S", 0)
    over = "dd if=/dev/zero of=big.bin bs=1M count=5 2>&1"
    r = await runner.run_command(tmp_path, over, sandbox=False)
    assert not r.ok  # 寫超過 1MB → SIGXFSZ/失敗
    monkeypatch.setattr(config, "RLIMITS_ENABLED", False)
    r2 = await runner.run_command(
        tmp_path, "dd if=/dev/zero of=ok.bin bs=1M count=5 2>&1", sandbox=False
    )
    assert r2.ok  # 停用後同指令成功


async def test_run_command_exec_as_limit_applied_to_child(tmp_path, enabled, monkeypatch):
    """驗證 run_command_exec(..., sandbox=False) 會正確套用資源限制。"""
    import sys
    monkeypatch.setattr(config, "RLIMIT_MEM_MB", 512)
    monkeypatch.setattr(config, "RLIMIT_CPU_S", 0)
    monkeypatch.setattr(config, "RLIMIT_FSIZE_MB", 0)
    r = await runner.run_command_exec(
        tmp_path,
        [sys.executable, "-c", "import resource; print(resource.getrlimit(resource.RLIMIT_AS)[0])"],
        sandbox=False
    )
    assert r.ok, r.output
    assert r.output.strip() == str(512 * 1024 * 1024)


async def test_run_command_exec_sandbox_true_passes_preexec(tmp_path, enabled, monkeypatch):
    """驗證 run_command_exec(..., sandbox=True) 會正確傳遞 preexec_fn 給 create_subprocess_exec。"""
    import asyncio
    monkeypatch.setattr(config, "_sandbox_available", lambda: True)
    monkeypatch.setattr(config, "RLIMIT_MEM_MB", 512)
    
    called_kwargs = {}
    
    async def mock_create_subprocess_exec(*args, **kwargs):
        called_kwargs.update(kwargs)
        class MockProcess:
            returncode = 0
            pid = 99999
            async def communicate(self):
                return b"mocked output", b""
            async def wait(self):
                return 0
        return MockProcess()
        
    monkeypatch.setattr(asyncio, "create_subprocess_exec", mock_create_subprocess_exec)
    
    r = await runner.run_command_exec(tmp_path, ["python3", "-c", "print(1)"], sandbox=True)
    assert r.ok
    assert "preexec_fn" in called_kwargs
    assert called_kwargs["preexec_fn"] is not None

