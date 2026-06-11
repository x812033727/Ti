"""自我改進機制的設定預設（穩健式）與閘門 helper —— env-only，不入設定面板。

與 TI_LESSONS／NOTES／HUDDLE／CRITIC 一致屬「啟動時固定」的進階開關，故以乾淨子程序環境
驗證預設值（避免本測試行程的 env 污染），不走 settings.FIELDS／reload。
"""

from __future__ import annotations

import os
import subprocess
import sys

from studio import config


def test_objective_gate_helpers(monkeypatch):
    monkeypatch.setattr(config, "OBJECTIVE_GATE", "0")
    assert config.objective_gate_enabled() is False and config.objective_gate_strict() is False
    monkeypatch.setattr(config, "OBJECTIVE_GATE", "1")
    assert config.objective_gate_enabled() is True and config.objective_gate_strict() is False
    monkeypatch.setattr(config, "OBJECTIVE_GATE", "strict")
    assert config.objective_gate_enabled() is True and config.objective_gate_strict() is True


def test_not_exposed_in_settings_panel():
    """進階開關刻意不進設定面板（與 TI_LESSONS／HUDDLE 等一致）。"""
    from studio import settings

    envs = {f.env for f in settings.FIELDS}
    assert not ({"TI_REFLEXION", "TI_OBJECTIVE_GATE", "TI_SELF_REFINE_ITERS", "TI_RLIMITS"} & envs)


def test_stable_defaults_in_isolated_env(tmp_path):
    """乾淨環境（無 TI_* 覆寫、無 .env）下確認穩健式預設：C 開、A／B／D 關。"""
    repo_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    env = {k: v for k, v in os.environ.items() if not k.startswith("TI_")}
    env["PYTHONPATH"] = repo_root  # 子程序找得到 studio
    code = (
        "import studio.config as c;"
        "print(c.RLIMITS_ENABLED, c.REFLEXION_ENABLED, repr(c.OBJECTIVE_GATE),"
        " c.SELF_REFINE_ITERS, c.RLIMIT_MEM_MB, c.RLIMIT_CPU_S, c.RLIMIT_FSIZE_MB)"
    )
    # cwd=tmp_path 避免讀到 repo 的 .env（dotenv 從 cwd 找）。
    r = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, env=env, cwd=tmp_path
    )
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "True False '0' 0 4096 300 512"
