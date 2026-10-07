"""QA：驗證 `_bwrap_prefix` 的 `--ro-bind / /` 真的把主機路徑掛成唯讀。

需求：沙箱內對唯讀掛載區（host 根，被 `--ro-bind / /` 蓋）寫入應被拒，
而對 `--bind cwd cwd` 開放的可寫區寫入應成功——兩案並陳才證明「是
ro-bind 在擋，而非整個沙箱寫不進」。

設計重點：
  * 唯一接點是 `runner._bwrap_prefix(ws)`，所有 bwrap flag（含 net/profile
    修正）都從這裡繼承，測試端絕不複寫任何 flag。
  * 全程真實 `subprocess.run` 實跑，不 mock；無 bwrap 時 skip。
  * NET 走 `monkeypatch.setattr(runner.config, "SANDBOX_NET", True)`（等同
    TI_SANDBOX_NET=1），繞開受限 runner 的 `--unshare-net` loopback EPERM。
  * ro-bind 反例寫入 `/etc/.ti_ro_bind_probe_*`：該路徑在沙箱內由
    `--ro-bind / /` 提供，可避免 workspace 位於 `/tmp` 時被 `--tmpfs /tmp`
    遮住而變成 ENOENT。此路徑出現 EROFS 即確證 ro-bind 生效。
"""

import errno
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from studio import runner

TIMEOUT = 30  # 秒；避免 bwrap 卡死拖垮 CI

needs_bwrap = pytest.mark.skipif(
    shutil.which("bwrap") is None,
    reason="本機/CI 無 bwrap，略過 ro-bind 實跑測試",
)


@pytest.fixture
def ro_probe_path():
    """挑一個沙箱內由 ro-bind 可見、且不會被 `/tmp` tmpfs 遮掉的探針路徑。"""
    probe = Path(f"/etc/.ti_ro_bind_probe_{os.getpid()}.txt")
    assert not probe.exists(), f"測試探針路徑不應已存在：{probe}"
    try:
        yield probe
    finally:
        try:
            probe.unlink(missing_ok=True)
        except OSError as exc:
            if exc.errno != errno.EROFS:
                raise


@needs_bwrap
def test_ro_bind_rejects_write_to_host_path(ro_probe_path, tmp_path, monkeypatch):
    """反例：沙箱內寫 host 唯讀路徑應被拒，且 host 檔案未被竄改。"""
    monkeypatch.setattr(runner.config, "SANDBOX_NET", True)

    # 沙箱內以繼承前綴寫 host rootfs 路徑——應被 ro-bind 擋下。
    prefix = runner._bwrap_prefix(tmp_path)
    r = subprocess.run(
        prefix + ["bash", "-c", f"echo TAINT > {ro_probe_path}"],
        capture_output=True,
        text=True,
        timeout=TIMEOUT,
    )
    assert r.returncode != 0, f"寫唯讀區應失敗，實際 rc={r.returncode}\n{r.stderr}"
    assert "Read-only file system" in r.stderr, (
        f"應為 EROFS（ro-bind 生效），實際 stderr：{r.stderr!r}"
    )

    # 雙重保險：回 host 確認沒有產生探針檔（確實沒寫進去）。
    assert not ro_probe_path.exists(), "host rootfs 不應被沙箱寫入探針檔"


@needs_bwrap
def test_bind_allows_write_to_writable_area(tmp_path, monkeypatch):
    """正例：同前綴寫 `--bind` 可寫區（tmp_path）應成功——證明擋的是 ro-bind，
    而非整個沙箱起不來或寫不進；也堵住「bwrap 整個失敗導致反例假通過」的盲點。"""
    monkeypatch.setattr(runner.config, "SANDBOX_NET", True)

    prefix = runner._bwrap_prefix(tmp_path)
    target = tmp_path / "ok.txt"
    r = subprocess.run(
        prefix + ["bash", "-c", f"echo OK > {target}"],
        capture_output=True,
        text=True,
        timeout=TIMEOUT,
    )
    assert r.returncode == 0, f"可寫區寫入應成功，實際 rc={r.returncode}\n{r.stderr}"
    assert target.exists(), "寫入後 host 端應能看到該檔（--bind 連回主機）"
    assert target.read_text().strip() == "OK"
