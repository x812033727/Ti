"""QA 任務#3：驗證 ci.yml sandbox-test job 的 bwrap smoke 前置步驟為「安全閥」。

驗收 #3：smoke 成功 exit 0；profile 未生效（bwrap 不可用）時 job 在 smoke
步驟即失敗（exit≠0）且訊息清楚——而非靜默 skip / fail-open。

本機 sandbox 有 bwrap，故可同時驗：
  (結構) smoke 步驟存在、排在跑測試步驟之前、無 continue-on-error、
         失敗時 exit 1 + 指向 AppArmor profile 的訊息、flag 對齊實跑路徑。
  (行為) 抽出 smoke 的 shell 邏輯，實跑成功路徑(exit 0)與
         模擬 bwrap 不可用的失敗路徑(exit 1 + 含 ::error:: 訊息)。
"""

import shutil
import subprocess

import pytest
from _repo import REPO_ROOT

yaml = pytest.importorskip("yaml")

CI = REPO_ROOT / ".github" / "workflows" / "ci.yml"


@pytest.fixture(scope="module")
def job():
    return yaml.safe_load(CI.read_text())["jobs"]["sandbox-test"]


def _names(job):
    return [s.get("name", "") for s in job["steps"]]


def _step(job, key):
    hit = [s for s in job["steps"] if key.lower() in s.get("name", "").lower()]
    assert hit, f"找不到名稱含 {key!r} 的步驟"
    return hit[0]


def _code(run: str) -> str:
    return "\n".join(ln for ln in run.splitlines() if not ln.strip().startswith("#"))


# --- 結構 ---------------------------------------------------------------


def test_smoke_step_exists(job):
    assert any("smoke" in n.lower() for n in _names(job)), "缺 bwrap smoke 步驟"


def test_smoke_runs_before_tests(job):
    """smoke 必須排在跑沙箱測試之前，才能在 profile 失效時先攔下。"""
    names = [n.lower() for n in _names(job)]
    smoke_i = next(i for i, n in enumerate(names) if "smoke" in n)
    test_i = next(i for i, n in enumerate(names) if "run sandbox tests" in n)
    assert smoke_i < test_i, "smoke 步驟必須在 Run sandbox tests 之前"


def test_smoke_no_continue_on_error(job):
    """安全閥不可被 continue-on-error 旁路。"""
    smoke = _step(job, "smoke")
    assert smoke.get("continue-on-error") is not True


def test_smoke_fails_with_clear_message(job):
    """失敗路徑：exit 1 且訊息明確指向 AppArmor profile 未生效。"""
    code = _code(_step(job, "smoke")["run"])
    assert "exit 1" in code, "smoke 失敗須 exit 1（讓 job 早失敗）"
    assert "::error::" in code, "失敗須印 GitHub Actions error 註記"
    assert ("AppArmor" in code) or ("profile" in code), "訊息須點名 AppArmor/profile"


def test_smoke_flags_align_real_run(job):
    """flag 對齊實跑 _bwrap_prefix：含 --unshare-pid，不含 --unshare-net（NET=1 路徑）。"""
    code = _code(_step(job, "smoke")["run"])
    assert "bwrap" in code and "--unshare-pid" in code
    assert "--unshare-net" not in code, "NET=1 實跑不 unshare-net，smoke 也不該"
    assert "--ro-bind / /" in code, "smoke 應對齊 host 唯讀掛載"


# --- 行為（本機實跑）----------------------------------------------------

needs_bwrap = pytest.mark.skipif(
    shutil.which("bwrap") is None, reason="本機無 bwrap，略過 smoke 行為實跑"
)


@needs_bwrap
def test_smoke_command_succeeds_locally(job):
    """成功路徑：直接跑整段 smoke 步驟 body，本機 bwrap 可用時應 exit 0 並印 OK。"""
    run = _step(job, "smoke")["run"]  # 含 ws=$(mktemp -d) 前置，原樣執行最忠實
    r = subprocess.run(["bash", "-c", run], capture_output=True, text=True)
    assert r.returncode == 0, f"smoke 本機應 exit 0，實際 {r.returncode}\n{r.stderr}"
    assert "bwrap smoke OK" in r.stdout, f"成功應印 OK：{r.stdout}"


@needs_bwrap
def test_smoke_gate_fails_when_bwrap_unusable(tmp_path):
    """失敗路徑模擬：bwrap 不可用時，smoke 邏輯應 exit 1 並印 ::error::。

    用一支假的 bwrap（永遠 exit 1）放到 PATH 前面，重現「profile 未生效→bwrap
    執行失敗」，驗證 gate 的 if/exit 1/訊息分支真的會擋下。
    """
    fake = tmp_path / "bwrap"
    fake.write_text("#!/bin/sh\nexit 1\n")
    fake.chmod(0o755)
    # 與 ci.yml 同構的 gate 片段（行為等價）
    gate = (
        "if ! bwrap --ro-bind / / --unshare-pid --die-with-parent --new-session true; then\n"
        '  echo "::error::bwrap smoke 失敗——AppArmor userns profile 未生效，沙箱不可用。"\n'
        "  exit 1\n"
        "fi\n"
        'echo "bwrap smoke OK"\n'
    )
    env = {"PATH": f"{tmp_path}:/usr/bin:/bin"}
    r = subprocess.run(["bash", "-c", gate], capture_output=True, text=True, env=env)
    assert r.returncode == 1, "bwrap 不可用時 gate 必須 exit 1（不可放行）"
    assert "::error::" in r.stdout and "profile" in r.stdout, "失敗訊息須清楚指向 profile"
    assert "bwrap smoke OK" not in r.stdout, "失敗時不應印出成功訊息"
