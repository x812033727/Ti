"""QA 任務#4：sandbox-test job 的環境設定須讓沙箱實跑「走 --unshare-pid、
不 --unshare-net」以繞開 Ubuntu 24.04 受限 runner 的 loopback EPERM。

⚠️ 重要矛盾（驗收 #4 字面 vs 代碼真相）：
驗收 #4 字面寫「不設 TI_SANDBOX_NET ... 不 --unshare-net」，但現行
studio/runner.py:96-97 是：
    if not config.SANDBOX_NET:        # 不設 → SANDBOX_NET=False
        args.append("--unshare-net")  # → 反而會加 --unshare-net → 觸發 EPERM
故「不設 NET」實際會【加】--unshare-net，與目標相反。要達成「不 --unshare-net」
唯一正確設定是 TI_SANDBOX_NET=1（架構修正定案）。本測試以實際代碼鐵證此因果，
並驗 ci.yml 採 NET=1。
"""
import importlib
import pathlib

import pytest

yaml = pytest.importorskip("yaml")

from studio import runner  # noqa: E402

CI = pathlib.Path(__file__).resolve().parent.parent / ".github" / "workflows" / "ci.yml"


@pytest.fixture(scope="module")
def run_step():
    d = yaml.safe_load(CI.read_text())
    steps = d["jobs"]["sandbox-test"]["steps"]
    hit = [s for s in steps if "run sandbox tests" in s.get("name", "").lower()]
    assert hit, "找不到 Run sandbox tests 步驟"
    return hit[0]


# --- 代碼鐵證：_bwrap_prefix 的 --unshare-net 由 SANDBOX_NET 決定 ---------


def test_unshare_net_added_when_net_false(tmp_path, monkeypatch):
    """SANDBOX_NET=False（= 不設 TI_SANDBOX_NET）→ 會加 --unshare-net（EPERM 陷阱）。"""
    monkeypatch.setattr(runner.config, "SANDBOX_NET", False)
    args = runner._bwrap_prefix(tmp_path)
    assert "--unshare-net" in args, "不設 NET 時現行代碼會加 --unshare-net"
    assert "--unshare-pid" in args


def test_unshare_net_absent_when_net_true(tmp_path, monkeypatch):
    """SANDBOX_NET=True（= TI_SANDBOX_NET=1）→ 不加 --unshare-net（繞開 EPERM）。"""
    monkeypatch.setattr(runner.config, "SANDBOX_NET", True)
    args = runner._bwrap_prefix(tmp_path)
    assert "--unshare-net" not in args, "NET=1 時不應加 --unshare-net"
    assert "--unshare-pid" in args, "PID 隔離仍須保留（保護主機）"


def test_config_parses_net_env_truthy(monkeypatch):
    """config 讀 TI_SANDBOX_NET=1 → SANDBOX_NET=True；不設/0 → False。"""
    import studio.config as cfg

    monkeypatch.setenv("TI_SANDBOX_NET", "1")
    c1 = importlib.reload(cfg)
    assert c1.SANDBOX_NET is True

    monkeypatch.setenv("TI_SANDBOX_NET", "0")
    c2 = importlib.reload(cfg)
    assert c2.SANDBOX_NET is False

    monkeypatch.delenv("TI_SANDBOX_NET", raising=False)
    c3 = importlib.reload(cfg)
    assert c3.SANDBOX_NET is False  # 不設 → False → 會 --unshare-net
    # 還原模組狀態給其他測試
    importlib.reload(cfg)


# --- ci.yml 設定：對齊「繞開 EPERM」的唯一正確值 ------------------------


def test_ci_sets_sandbox_enabled(run_step):
    """驗收 #4：TI_SANDBOX=1 生效（開沙箱）。"""
    assert run_step.get("env", {}).get("TI_SANDBOX") == "1"


def test_ci_sets_net_for_correct_flag(run_step):
    """繞開 loopback EPERM 的唯一正確設定：TI_SANDBOX_NET=1。

    注意：這與驗收 #4『不設 NET』字面相反，但與架構修正定案一致——
    現行代碼下唯有 NET=1 才不會 append --unshare-net。
    """
    env = run_step.get("env", {})
    assert env.get("TI_SANDBOX_NET") == "1", (
        "現行 runner._bwrap_prefix 唯有 SANDBOX_NET=True 才不加 --unshare-net；"
        "若不設或設 0，沙箱會 --unshare-net 在受限 runner 觸發 loopback EPERM。"
    )
