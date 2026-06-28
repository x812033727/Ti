"""發佈前自動排版（runner.ruff_format_workspace）的單元測試。

改良迴圈無 lint 閘門，未排版交付碼會被目標 repo 的 CI（ruff format --check）擋住；此步在
發佈前對 ruff 專案自動排版補上缺口。驗證：ruff 專案會被排版、非 ruff/無 .git 不動、best-effort。
"""

from __future__ import annotations

import pytest

from studio import config, runner


@pytest.fixture(autouse=True)
def _enable_git(monkeypatch):
    monkeypatch.setattr(config, "ENABLE_GIT", True)


async def test_formats_ruff_project(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "pyproject.toml").write_text("[tool.ruff]\nline-length = 100\n", encoding="utf-8")
    f = tmp_path / "x.py"
    f.write_text("x=1\ny =  2\n", encoding="utf-8")  # 未排版
    await runner.ruff_format_workspace(tmp_path)
    assert f.read_text(encoding="utf-8") == "x = 1\ny = 2\n"  # ruff 已排版


async def test_skips_non_ruff_project(tmp_path):
    (tmp_path / ".git").mkdir()  # 有 git 但無 ruff 設定
    f = tmp_path / "x.py"
    orig = "x=1\ny =  2\n"
    f.write_text(orig, encoding="utf-8")
    await runner.ruff_format_workspace(tmp_path)
    assert f.read_text(encoding="utf-8") == orig  # 非 ruff 專案→不動


async def test_skips_without_git(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[tool.ruff]\n", encoding="utf-8")  # ruff 但無 .git
    f = tmp_path / "x.py"
    orig = "x=1\n"
    f.write_text(orig, encoding="utf-8")
    await runner.ruff_format_workspace(tmp_path)
    assert f.read_text(encoding="utf-8") == orig  # 無 .git（非發佈用 workspace）→不動


async def test_ruff_toml_also_detected(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "ruff.toml").write_text("line-length = 100\n", encoding="utf-8")
    f = tmp_path / "x.py"
    f.write_text("x=1\n", encoding="utf-8")
    await runner.ruff_format_workspace(tmp_path)
    assert f.read_text(encoding="utf-8") == "x = 1\n"
