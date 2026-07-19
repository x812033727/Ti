"""任務 #3 驗收測試：不汙染現有流程。

驗證新增的 shell 掃描（S602/S604/S605 + create_subprocess_shell grep）
未滲入主流程：
  - 主 pyproject [tool.ruff.lint].select 不含任何 S 規則
  - `ruff check .` 仍全綠，且輸出不含 S60x（新規則沒被主 lint 套用）
  - `ruff format --check .` 仍全綠
  - 對照：S60x 規則只在 --isolated 掃描下生效，沒進主 lint
"""

import os
import re
import subprocess
import sys
import tomllib

import pytest
from _repo import REPO_ROOT

REPO = REPO_ROOT
PYPROJECT = REPO / "pyproject.toml"


def run(args):
    argv = list(args)
    if argv and argv[0] == "ruff":
        argv = [sys.executable, "-m", "ruff", *argv[1:]]
    return subprocess.run(
        argv,
        cwd=REPO,
        env=dict(os.environ),
        capture_output=True,
        text=True,
    )


# --- 靜態：主 ruff 設定不含 S 規則 ----------------------------------------


def test_main_ruff_select_excludes_shell_rules():
    cfg = tomllib.loads(PYPROJECT.read_text())
    select = cfg["tool"]["ruff"]["lint"]["select"]
    # 主 lint 不該直接含 S60x，也不該用會「涵蓋」S 的籠統前綴（"S" / "ALL"）。
    bad = [r for r in select if r in ("S602", "S604", "S605", "S", "ALL")]
    assert not bad, f"主 ruff select 汙染了 shell 規則：{bad}（select={select}）"


def test_main_ruff_extend_select_absent_or_clean():
    """若有 extend-select / per-file-ignores，也不得偷渡 S 規則。"""
    cfg = tomllib.loads(PYPROJECT.read_text())
    lint = cfg["tool"]["ruff"]["lint"]
    extend = lint.get("extend-select", [])
    bad = [r for r in extend if r.startswith("S") or r == "ALL"]
    assert not bad, f"extend-select 偷渡了 S 規則：{bad}"


# --- 動態：主 lint / format 行為不變 --------------------------------------


def test_ruff_check_dot_passes_without_s_rules():
    cp = run(["ruff", "check", ".", "--output-format", "concise"])
    out = cp.stdout + cp.stderr
    assert cp.returncode == 0, f"ruff check . 不再全綠（新規則汙染？）：\n{out}"
    # 即使 studio 內有 shell=True，主 lint 也不該冒出 S60x
    assert not re.search(r"\bS60[245]\b", out), f"ruff check . 冒出 S60x（被汙染）：\n{out}"


def test_ruff_format_check_dot_passes():
    cp = run(["ruff", "format", "--check", "."])
    out = cp.stdout + cp.stderr
    assert cp.returncode == 0, f"ruff format --check . 不再全綠：\n{out}"


# --- 對照：S 規則只活在 --isolated 掃描，沒進主 lint -----------------------


def test_s_rules_only_fire_under_isolated_scan():
    """同一份 studio/：主設定不報 S60x；--isolated --select 才報。
    證明新規則被隔離在掃描專用路徑，未進入 `ruff check .`。"""
    # (a) 主設定掃 studio（沿用 pyproject）：不應有 S60x
    main_cp = run(["ruff", "check", "studio", "--output-format", "concise"])
    main_has_s = bool(re.search(r"\bS60[245]\b", main_cp.stdout + main_cp.stderr))
    assert not main_has_s, "主設定竟報出 S60x，代表規則已汙染主 lint"

    # (b) isolated + select：studio/runner.py 等若有 shell 用法則應報
    iso_cp = run(
        [
            "ruff",
            "check",
            "--isolated",
            "--select",
            "S602,S604,S605",
            "studio",
            "--output-format",
            "concise",
        ]
    )
    iso_out = iso_cp.stdout + iso_cp.stderr
    # 注意：studio 目前 shell=True 命中可能為 0（多已改 list 形式），
    # 故此處不強制要有命中，只驗「isolated 路徑可獨立運作且與主 lint 分離」。
    # 關鍵不變式：isolated 的規則集 != 主 lint 的規則集。
    cfg = tomllib.loads(PYPROJECT.read_text())
    main_select = set(cfg["tool"]["ruff"]["lint"]["select"])
    assert not ({"S602", "S604", "S605"} & main_select), "規則集未分離"
    assert iso_cp.returncode in (0, 1), f"isolated 掃描異常退出：\n{iso_out}"


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
