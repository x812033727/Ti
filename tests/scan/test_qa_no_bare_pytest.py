"""QA 驗證：禁止文件出現裸 pytest 指令（任務 #1 / no-bare-pytest hook）。

對齊驗收標準逐條釘死：
  #1 .pre-commit-config.yaml 有 repo:local hook id=no-bare-pytest、files 限定 docs/
  #2 含裸 `pytest tests/` 的檔案 → exit≠0 + 可讀錯誤訊息（指引 python -m pytest）
  #3 白名單寫法零誤殺
  #5 CI 與本地共用同一規則來源（不另寫 grep）
雙路徑（有 rg / 無 rg 強制 fallback）皆覆蓋——對齊架構決策。
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "scan_bare_pytest.sh"
CONFIG = REPO / ".pre-commit-config.yaml"
CI = REPO / ".github" / "workflows" / "ci.yml"


def run_scan(targets, *, force_fallback=False, mode="block"):
    """跑掃描腳本；force_fallback=True 時藏掉 rg 逼走 grep 路徑。"""
    env = dict(os.environ, SCAN_MODE=mode)
    if force_fallback:
        # 造一個只放安全工具、不含 rg 的 PATH，逼腳本走 grep fallback。
        binstub = REPO / ".qa-tmp-nopath"
        binstub.mkdir(exist_ok=True)
        for tool in ("bash", "grep", "env", "sh", "cat", "printf"):
            real = shutil.which(tool)
            if real:
                link = binstub / tool
                if not link.exists():
                    link.symlink_to(real)
        env["PATH"] = str(binstub)
    return subprocess.run(
        ["bash", str(SCRIPT), *targets],
        capture_output=True, text=True, env=env, cwd=REPO,
    )


# ---- 黑樣本：必須被攔（行首 / 句中 / inline code）-------------------------
BLACK_SAMPLES = [
    "pytest tests/",                                  # 行首指令
    "- **完整套件** `pytest tests/` → 8 failed",      # inline code（0002 第19行型）
    "請執行 pytest tests/test_foo.py -q 驗證",          # 句中非行首
    "    pytest -q tests/",                            # 縮排指令
    "pytest foo.py",                                   # 直接帶 .py
]

# ---- 白名單：零誤殺 ------------------------------------------------------
WHITE_SAMPLES = [
    "python -m pytest tests/",
    ".venv/bin/python -m pytest tests/ -q",
    "uv run pytest tests/",
    "poetry run pytest tests/",
    "@pytest.fixture",
    "@pytest.mark.asyncio",
    "見 pytest.ini 設定",
    "本專案使用 pytest 套件進行測試",                   # 行內提及套件名
    "安裝 pytest-asyncio 與 pytest-cov",
]


@pytest.fixture
def docfile(tmp_path):
    def _make(text: str) -> str:
        f = tmp_path / "sample.md"
        f.write_text(text + "\n", encoding="utf-8")
        return str(f)
    return _make


@pytest.mark.parametrize("sample", BLACK_SAMPLES)
@pytest.mark.parametrize("fallback", [False, True], ids=["rg", "grep-fallback"])
def test_black_samples_blocked(docfile, sample, fallback):
    """驗收#2：黑樣本 → exit≠0 且印出可讀指引訊息。雙路徑一致。"""
    r = run_scan([docfile(sample)], force_fallback=fallback)
    assert r.returncode != 0, f"應被攔卻通過: {sample!r}\n{r.stdout}{r.stderr}"
    assert "python -m pytest" in r.stderr, f"缺少友善指引訊息: {r.stderr!r}"


@pytest.mark.parametrize("sample", WHITE_SAMPLES)
@pytest.mark.parametrize("fallback", [False, True], ids=["rg", "grep-fallback"])
def test_white_samples_pass(docfile, sample, fallback):
    """驗收#3：白名單零誤殺，exit==0。雙路徑一致。"""
    r = run_scan([docfile(sample)], force_fallback=fallback)
    assert r.returncode == 0, f"白名單被誤殺: {sample!r}\n{r.stdout}{r.stderr}"


def test_config_has_hook():
    """驗收#1：config 有 repo:local hook id=no-bare-pytest、files 限定 docs/。"""
    data = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    local = [r for r in data["repos"] if r.get("repo") == "local"]
    hooks = [h for repo in local for h in repo.get("hooks", [])]
    hook = next((h for h in hooks if h.get("id") == "no-bare-pytest"), None)
    assert hook is not None, "找不到 id=no-bare-pytest 的 repo:local hook"
    assert "docs" in hook.get("files", ""), f"files 未限定 docs/: {hook.get('files')!r}"


def _ci_pytest_step():
    data = yaml.safe_load(CI.read_text(encoding="utf-8"))
    for step in data["jobs"]["lint"]["steps"]:
        if "scan_bare_pytest" in str(step.get("run", "")):
            return step
    return None


def test_ci_uses_same_source():
    """驗收#5：CI 須執行同一條檢查（共用 SSOT 腳本），不另寫重複 grep。"""
    step = _ci_pytest_step()
    assert step is not None, "CI lint job 未接入 scan_bare_pytest.sh（criterion #5 未達成）"
    # 共用同一支腳本：CI run 與 hook entry 指向相同 SSOT 檔。
    cfg = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    hook_entry = next(
        h["entry"] for r in cfg["repos"] if r.get("repo") == "local"
        for h in r.get("hooks", []) if h.get("id") == "no-bare-pytest"
    )
    assert "scan_bare_pytest.sh" in hook_entry, "hook 未用 SSOT 腳本"
    assert "scan_bare_pytest.sh" in step["run"], "CI 未用同一 SSOT 腳本"


def test_ci_no_duplicate_grep():
    """驗收#5：CI 不得另寫獨立 grep/rg 掃 pytest（規則單一來源）。"""
    import re
    text = CI.read_text(encoding="utf-8")
    bad = [
        ln for ln in text.splitlines()
        if re.search(r"(grep|rg)\b.*pytest", ln) and "scan_bare_pytest" not in ln
    ]
    assert not bad, f"CI 出現自寫 grep/rg pytest 邏輯（違反單一來源）: {bad}"


def test_ci_pytest_step_is_blocking():
    """驗收#5 配套：CI pytest 掃描須為 blocking（不得 continue-on-error 吞掉命中）。"""
    step = _ci_pytest_step()
    assert step is not None
    assert step.get("continue-on-error", False) is False, (
        "CI pytest 掃描被設為 continue-on-error，命中不會擋 CI"
    )


def test_real_docs_clean():
    """驗收#4：實際 docs/ 全量掃描須零違規（既有違規已修，hook 全綠）。"""
    r = run_scan(["docs"])
    assert r.returncode == 0, f"docs/ 仍有裸 pytest 違規:\n{r.stderr}"


def test_warn_mode_never_blocks(docfile):
    """家族逃生口：SCAN_MODE=warn 即使命中也回 0。"""
    r = run_scan([docfile("pytest tests/")], mode="warn")
    assert r.returncode == 0
