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
  * 「host 預置可寫檔」策略隔離變因：探針檔建在沙箱內仍會被 `--ro-bind / /`
    看見、且不被 `--tmpfs /tmp`、`~/.cache`、cwd bind 覆蓋的位置。先在 host 證其
    可寫，排除「該路徑本來就不可寫（權限）」的假陽性；此前提下沙箱內出現 EROFS
    即確證 ro-bind 生效。
"""

import os
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from studio import runner

TIMEOUT = 30  # 秒；避免 bwrap 卡死拖垮 CI

needs_bwrap = pytest.mark.skipif(
    shutil.which("bwrap") is None,
    reason="本機/CI 無 bwrap，略過 ro-bind 實跑測試",
)


def _is_relative_to(path: Path, root: Path) -> bool:
    return path == root or path.is_relative_to(root)


def _visible_host_writable_dir() -> Path:
    """找一個會被 ro-bind 看見、且 host 端可寫的 probe 目錄。"""
    masked_roots = [Path("/tmp").resolve(), (Path.home() / ".cache").resolve()]
    for candidate in (Path("/var/tmp"), Path.home()):
        try:
            root = candidate.resolve()
        except OSError:
            continue
        if any(_is_relative_to(root, masked) for masked in masked_roots):
            continue
        if root.is_dir() and os.access(root, os.W_OK | os.X_OK):
            return root
    pytest.skip("找不到沙箱內可見且 host 端可寫的 ro-bind 探針目錄")


@pytest.fixture
def host_probe():
    """在 ro-bind 可見的 host 可寫區預置探針檔，測試後清理。

    刻意避開 `/tmp`、`~/.cache`、cwd——前兩者在沙箱內是 tmpfs，cwd 是 `--bind`
    可寫，都無法驗到唯讀。
    """
    fd, name = tempfile.mkstemp(
        prefix=f".ti_ro_bind_probe_{os.getpid()}_",
        suffix=".txt",
        dir=_visible_host_writable_dir(),
        text=True,
    )
    probe = Path(name)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("ORIG")
    try:
        yield probe
    finally:
        probe.unlink(missing_ok=True)


@needs_bwrap
def test_ro_bind_rejects_write_to_host_path(host_probe, tmp_path, monkeypatch):
    """反例：沙箱內寫 host 唯讀路徑應被拒，且 host 檔案未被竄改。"""
    monkeypatch.setattr(runner.config, "SANDBOX_NET", True)

    # ① 前置：該路徑在 host 原生可寫（fixture 已寫入 ORIG），排除權限假陽性。
    assert host_probe.read_text() == "ORIG", "前置：host 探針應已預置且可讀"

    # ② 沙箱內以繼承前綴寫同一絕對路徑——應被 ro-bind 擋下。
    prefix = runner._bwrap_prefix(tmp_path)
    r = subprocess.run(
        prefix + ["bash", "-c", f"echo TAINT > {shlex.quote(str(host_probe))}"],
        capture_output=True,
        text=True,
        timeout=TIMEOUT,
    )
    assert r.returncode != 0, f"寫唯讀區應失敗，實際 rc={r.returncode}\n{r.stderr}"
    assert "Read-only file system" in r.stderr, (
        f"應為 EROFS（ro-bind 生效），實際 stderr：{r.stderr!r}"
    )

    # ③ 雙重保險：回 host 確認內容維持 ORIG（確實沒寫進去）。
    assert host_probe.read_text() == "ORIG", "host 探針內容不應被沙箱竄改"


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
