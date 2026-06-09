"""QA 任務#1：驗證 ci.yml 的 sandbox-test job 符合驗收標準（結構面）。

只驗 CI 設定的結構與不變式，沙箱實跑由 tests/core/test_runner.py 等負責。
"""

import pytest
from _repo import REPO_ROOT

yaml = pytest.importorskip("yaml")

CI = REPO_ROOT / ".github" / "workflows" / "ci.yml"


@pytest.fixture(scope="module")
def doc():
    return yaml.safe_load(CI.read_text())


def test_yaml_valid_and_jobs_present(doc):
    """驗收6：YAML 合法可解析；三個 job 都在。"""
    assert "jobs" in doc
    for j in ("lint", "test", "sandbox-test"):
        assert j in doc["jobs"], f"缺少 job: {j}"


def test_test_job_unchanged_sandbox_off(doc):
    """驗收1：原 test job 仍 TI_SANDBOX=0，沙箱維持關閉。"""
    steps = doc["jobs"]["test"]["steps"]
    run_steps = [s for s in steps if "Run tests" in s.get("name", "")]
    assert run_steps, "test job 找不到 Run tests 步驟"
    assert run_steps[0].get("env", {}).get("TI_SANDBOX") == "0"


def test_sandbox_job_runs_on_ubuntu(doc):
    """驗收1：sandbox-test 在 ubuntu-latest。"""
    assert doc["jobs"]["sandbox-test"]["runs-on"] == "ubuntu-latest"


def test_sandbox_job_env_net_enabled(doc):
    """驗收4：實跑步驟 TI_SANDBOX=1 且 TI_SANDBOX_NET=1（繞開 loopback EPERM）。"""
    steps = doc["jobs"]["sandbox-test"]["steps"]
    run = [s for s in steps if s.get("env", {}).get("TI_SANDBOX") == "1"]
    assert run, "找不到 TI_SANDBOX=1 的步驟"
    env = run[0]["env"]
    assert env.get("TI_SANDBOX") == "1"
    assert env.get("TI_SANDBOX_NET") == "1", "現行 runner 下必須 NET=1 才不 append --unshare-net"


def test_sandbox_job_has_smoke_step_no_continue_on_error(doc):
    """驗收3：有 bwrap smoke 安全閥，且未啟用 continue-on-error。"""
    steps = doc["jobs"]["sandbox-test"]["steps"]
    smoke = [s for s in steps if "smoke" in s.get("name", "").lower()]
    assert smoke, "缺少 bwrap smoke 步驟"
    s = smoke[0]
    assert s.get("continue-on-error") is not True, "smoke 不可 continue-on-error"
    assert "bwrap" in s["run"] and "exit 1" in s["run"], "smoke 應於失敗時 exit 1"


def _strip_comments(run: str) -> str:
    """只保留實際指令行，去掉 shell 註解（# 開頭）——避免註解文字誤判旗標。"""
    return "\n".join(ln for ln in run.splitlines() if not ln.strip().startswith("#"))


def test_smoke_flags_align_with_real_run(doc):
    """驗收3/4：smoke flag 對齊實跑路徑——含 --unshare-pid，不含 --unshare-net。"""
    steps = doc["jobs"]["sandbox-test"]["steps"]
    smoke = [s for s in steps if "smoke" in s.get("name", "").lower()][0]["run"]
    code = _strip_comments(smoke)  # 註解裡會提到 --unshare-net，需排除
    assert "--unshare-pid" in code
    assert "--unshare-net" not in code, "NET=1 實跑不 unshare-net，smoke 也不該"


def test_installs_socat_and_apparmor(doc):
    """裝 socat（否則 fail-open 假 PASS）與 apparmor 套件。"""
    steps = doc["jobs"]["sandbox-test"]["steps"]
    allrun = "\n".join(s.get("run", "") for s in steps)
    assert "socat" in allrun
    assert "bubblewrap" in allrun
    assert "apparmor" in allrun
    assert "apparmor_parser" in allrun


def test_fallback_profile_heredoc_closes_at_col0(doc):
    """備援 profile heredoc 用 <<'EOF'，閉合 EOF 必須落在行首才合法。"""
    steps = doc["jobs"]["sandbox-test"]["steps"]
    prof = [s for s in steps if "AppArmor" in s.get("name", "")][0]["run"]
    lines = prof.splitlines()
    assert any(ln == "EOF" for ln in lines), "閉合 EOF 未落在行首（heredoc 會壞）"
    assert "profile bwrap /usr/bin/bwrap" in prof
