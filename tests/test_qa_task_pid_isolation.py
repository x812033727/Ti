"""QA 任務：沙箱實跑驗證 `--unshare-pid` 確實隔離主機進程。

核心安全保證：bubblewrap 以 `--unshare-pid` 建新 PID namespace、並掛新的
`--proc /proc`，使沙箱內看不到、也殺不到主機進程。本檔走正式 async exec 路徑
（`runner.run_command_exec(..., sandbox=True)` + 真實 `_bwrap_prefix`），不自拼
argv、不用 shell 版。

gating（fail-closed）：
  - 外層 `@pytest.mark.skipif(shutil.which("bwrap") is None)`：環境無 bwrap → skip。
  - 內層 smoke：走實跑路徑跑 `true`，userns 被擋（CI/AppArmor）時 skip 而非 fail。

net flag 須知：沙箱實跑強制 `config.SANDBOX_NET=True`（不 append `--unshare-net`），
否則受限 runner 上 `--unshare-net` 觸發 loopback EPERM 使 bwrap 整個起不來。這與
「測試不發網路」是兩回事——本檔的進程清點純走 `/proc`，不依賴網路。
"""

from __future__ import annotations

import glob
import os
import re
import shutil
import subprocess
import sys
import textwrap

import pytest

from studio import config, runner

# 外層 gating：無 bwrap 一律 skip（非 pass、非 fail）。
needs_bwrap = pytest.mark.skipif(
    shutil.which("bwrap") is None, reason="本機無 bwrap，略過沙箱 PID 隔離實跑"
)

# 主機端標記進程的獨特、好認 argv（sleep <可辨識秒數>）。
_MARK_SECONDS = "31337"


@pytest.fixture
def net_enabled(monkeypatch):
    """強制 SANDBOX_NET=True：實跑不 append --unshare-net，繞開受限 runner 的 loopback EPERM。"""
    monkeypatch.setattr(config, "SANDBOX_NET", True)
    return True


@pytest.fixture
async def sandbox_cwd(tmp_path, net_enabled):
    """smoke gate：走實跑路徑跑 `true`，無法實跑（userns 被擋）時 fail-closed 為 skip。

    與被測路徑同源（同一 `run_command_exec` + 真實 `_bwrap_prefix`），日後改旗標不漂移。
    """
    smoke = await runner.run_command_exec(
        str(tmp_path), ["true"], timeout=30, sandbox=True, label="smoke"
    )
    if not smoke.ok:
        pytest.skip(
            f"bwrap 無法實跑（userns 被擋？）：exit={smoke.exit_code} out={smoke.output!r}"
        )
    return str(tmp_path)


@pytest.fixture
def marker_proc():
    """主機端 spawn 一個好認的標記進程，yield 後 try/finally 清理，確保不殘留。"""
    p = subprocess.Popen(["sleep", _MARK_SECONDS])
    try:
        yield p
    finally:
        p.terminate()
        try:
            p.wait(timeout=5)
        except Exception:
            p.kill()
            p.wait()


# --- 防呆斷言：prefix 同時帶 PID 隔離兩旗標 ------------------------------


def test_prefix_has_pid_isolation_flags(tmp_path, net_enabled):
    """`_bwrap_prefix` 必須同時含 `--unshare-pid` 與 `--proc /proc`。

    兩者皆出現即可，不要求相鄰（實際被 `--tmpfs` 隔開，要求相鄰會誤紅）。
    防日後拿掉 `--proc` 致沙箱沿用主機 /proc、PID 隔離靜默失效（最常見回歸點）。
    """
    args = runner._bwrap_prefix(tmp_path)
    assert "--unshare-pid" in args, "缺 --unshare-pid，PID namespace 未隔離"
    assert "--proc" in args, "缺 --proc，沙箱會沿用主機 /proc"
    i = args.index("--proc")
    assert args[i + 1] == "/proc", "--proc 後須緊跟掛載點 /proc"
    # NET=1 路徑：不應 append --unshare-net。
    assert "--unshare-net" not in args, "NET=1 實跑不該 unshare-net"


# --- 沙箱實跑：正向＋反向＋雙條件 ---------------------------------------


@needs_bwrap
async def test_sandbox_cannot_see_host_process(sandbox_cwd, marker_proc):
    """沙箱內看不到、殺不到主機標記進程；進程數遠小於主機。"""
    host_pid = marker_proc.pid

    # 正向對照：host 端看得到該標記進程（kill -0 不丟例外即存在）。
    os.kill(host_pid, 0)

    # 嚴謹：確認確實走 NET=1 路徑（否則隔離前提的環境假設不成立）。
    assert config.SANDBOX_NET is True

    # 沙箱內：數 /proc/[0-9]+ 取 PID 集合，並對 host PID 送 signal（預期 ProcessLookupError）。
    # 結構化單行回傳供 regex 解析，不依賴 ps。
    script = textwrap.dedent(
        f"""
        import os, glob
        pids = [int(os.path.basename(p)) for p in glob.glob("/proc/[0-9]*")]
        has_mark = 1 if {host_pid} in pids else 0
        try:
            os.kill({host_pid}, 0)
            kill = "ok"          # 找得到該 PID（隔離失敗）
        except ProcessLookupError:
            kill = "fail"        # 找不到該 PID（隔離成功）
        except PermissionError:
            kill = "ok"          # 看得到但無權，仍屬可見
        print("COUNT=%d;HAS_MARK=%d;KILL=%s" % (len(pids), has_mark, kill))
        """
    )
    r = await runner.run_command_exec(
        sandbox_cwd, [sys.executable, "-c", script], timeout=60, sandbox=True, label="pidscan"
    )
    assert r.ok, f"沙箱腳本未成功：exit={r.exit_code} out={r.output!r}"

    # 容忍 stdout 前後雜訊，regex 抓單行而非整串 ==。
    m = re.search(r"COUNT=(\d+);HAS_MARK=([01]);KILL=(ok|fail)", r.output)
    assert m, f"無法解析沙箱輸出：{r.output!r}"
    count, has_mark, kill = int(m.group(1)), int(m.group(2)), m.group(3)

    # 反向佐證：沙箱內對 host PID 送 signal 應失敗（找不到）。
    assert kill == "fail", f"沙箱內對 host PID {host_pid} 送 signal 不該成功"
    # 隔離：標記 PID 不在沙箱進程集合。
    assert has_mark == 0, f"沙箱內竟看得到 host 標記 PID {host_pid}"

    # 雙條件：沙箱進程數為個位數，且顯著小於主機——避免 PID 重編號碰撞與硬編脆弱閾值。
    host_count = len(glob.glob("/proc/[0-9]*"))
    assert count < 10, f"沙箱進程數 {count} 應為個位數（新 PID namespace 內只有沙箱自身進程）"
    assert count < host_count, f"沙箱進程數 {count} 應顯著小於主機 {host_count}"
