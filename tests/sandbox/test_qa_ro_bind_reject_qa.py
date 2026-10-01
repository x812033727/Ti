"""QA：驗證 `_bwrap_prefix` 的 `--ro-bind / /` 真的把主機根掛成唯讀。

需求：沙箱內 host 根（被 `--ro-bind / /` 蓋）必須是 read-only，而對
`--bind cwd cwd` 開放的可寫區寫入應成功——兩案並陳才證明「根掛載是唯讀，
但工作區仍可寫」。

設計重點：
  * 唯一接點是 `runner._bwrap_prefix(ws)`，所有 bwrap flag（含 net/profile
    修正）都從這裡繼承，測試端絕不複寫任何 flag。
  * 全程真實 `subprocess.run` 實跑，不 mock；無 bwrap 時 skip。
  * NET 走 `monkeypatch.setattr(runner.config, "SANDBOX_NET", True)`（等同
    TI_SANDBOX_NET=1），繞開受限 runner 的 `--unshare-net` loopback EPERM。
  * 用 `/proc/self/mountinfo` 直接檢查 sandbox 內 `/` 掛載 options 含 `ro`。
    不用「在 host 預置檔案再寫入」：本 repo 可能位於 `/tmp`，而 `_bwrap_prefix`
    會 `--tmpfs /tmp`，該探針在 sandbox 內會被遮住成 ENOENT，反而測到環境布局。
"""

import shutil
import subprocess

import pytest
from _repo import REPO_ROOT

from studio import runner

REPO = REPO_ROOT
TIMEOUT = 30  # 秒；避免 bwrap 卡死拖垮 CI

needs_bwrap = pytest.mark.skipif(
    shutil.which("bwrap") is None,
    reason="本機/CI 無 bwrap，略過 ro-bind 實跑測試",
)


@needs_bwrap
def test_ro_bind_mounts_host_root_read_only(tmp_path, monkeypatch):
    """反例側：沙箱內 host 根掛載必須是 read-only。

    直接讀 mountinfo，避免在工作目錄外建立/嘗試寫入探針檔；也避開 repo 位於
    `/tmp` 時被 `--tmpfs /tmp` 遮蔽而得到 ENOENT 的環境相依假紅。
    """
    monkeypatch.setattr(runner.config, "SANDBOX_NET", True)

    prefix = runner._bwrap_prefix(tmp_path)
    code = (
        "from pathlib import Path\n"
        "for line in Path('/proc/self/mountinfo').read_text().splitlines():\n"
        "    fields = line.split()\n"
        "    if len(fields) > 5 and fields[4] == '/':\n"
        "        print(fields[5])\n"
        "        break\n"
    )
    r = subprocess.run(
        prefix + ["python3", "-c", code],
        capture_output=True,
        text=True,
        timeout=TIMEOUT,
    )
    assert r.returncode == 0, f"讀 mountinfo 應成功，實際 rc={r.returncode}\n{r.stderr}"
    opts = set(r.stdout.strip().split(","))
    assert "ro" in opts, f"根掛載應為 read-only（ro-bind 生效），實際 options={r.stdout!r}"


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
