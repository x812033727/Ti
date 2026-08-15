"""config.reload() 的 RLIMIT 數值接線測試。"""

from __future__ import annotations

from studio import config


def test_reload_reads_rlimit_value_env(monkeypatch):
    """TI_RLIMIT_* 數值改動後，runner 讀到的全域限制需即時更新。"""
    monkeypatch.setenv("TI_RLIMIT_MEM_MB", "1234")
    monkeypatch.setenv("TI_RLIMIT_CPU_S", "45")
    monkeypatch.setenv("TI_RLIMIT_FSIZE_MB", "67")
    try:
        config.reload()
        assert config.RLIMIT_MEM_MB == 1234
        assert config.RLIMIT_CPU_S == 45
        assert config.RLIMIT_FSIZE_MB == 67
    finally:
        monkeypatch.delenv("TI_RLIMIT_MEM_MB", raising=False)
        monkeypatch.delenv("TI_RLIMIT_CPU_S", raising=False)
        monkeypatch.delenv("TI_RLIMIT_FSIZE_MB", raising=False)
        config.reload()  # 還原全域，避免污染其他測試
