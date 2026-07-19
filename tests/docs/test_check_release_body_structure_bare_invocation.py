from __future__ import annotations

import json
import os
import shutil
import subprocess
import tomllib
from collections.abc import Mapping
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CLEAN_PATH = "/usr/local/bin:/usr/bin:/bin"


def _copy_checker_fixture(fixture_root: Path) -> None:
    scripts_dir = fixture_root / "scripts"
    scripts_dir.mkdir()
    shutil.copy2(
        ROOT / "scripts" / "check_release_body_structure.py",
        scripts_dir / "check_release_body_structure.py",
    )

    shutil.copytree(
        ROOT / "studio",
        fixture_root / "studio",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    shutil.copy2(ROOT / "pyproject.toml", fixture_root / "pyproject.toml")


def _clean_env() -> dict[str, str]:
    env = {"PATH": CLEAN_PATH, "PYTHONNOUSERSITE": "1"}

    assert "PYTHONPATH" not in env
    assert str(ROOT) not in os.pathsep.join(env.values())
    return env


def _project_version() -> str:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return data["project"]["version"]


def _write_minimal_evidence(fixture_root: Path) -> None:
    body = "\n".join(
        [
            "# Release 0.2.0",
            "",
            "## ⚠️ Breaking Changes",
            "",
            "- **① 行為變動**：strict 預設。",
            "- **② 原因**：防 symlink，且必須是 root ownership。",
            "- **③ before / after**：之前放行，之後改 strict。",
            f"- **④ 生效版本**：自 `{_project_version()}` 起。",
            "",
            "TI_REQUIRE_CHOWN=warn",
            "TI_REQUIRE_CHOWN=off",
        ]
    )
    evidence = {
        "body_match": True,
        "gh_release_view": {"body": body},
        "rest_release_by_tag_subset": {"body": body},
    }
    evidence_dir = fixture_root / "docs" / "evidence"
    evidence_dir.mkdir(parents=True)
    (evidence_dir / "release-v0.2.0-online-body.json").write_text(
        json.dumps(evidence, ensure_ascii=False),
        encoding="utf-8",
    )


def _run_checker(
    fixture_root: Path, env: Mapping[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["python3", "scripts/check_release_body_structure.py"],
        cwd=fixture_root,
        env=dict(env or _clean_env()),
        text=True,
        capture_output=True,
        check=False,
    )
    return result


def test_bare_invocation_bootstraps_imports_without_pythonpath(tmp_path: Path) -> None:
    _copy_checker_fixture(tmp_path)
    _write_minimal_evidence(tmp_path)

    result = _run_checker(tmp_path)

    assert result.returncode == 0, result.stderr
    assert "ModuleNotFoundError" not in result.stderr
    assert "== v0.2.0 線上 body" in result.stdout or "結構斷言核對" in result.stdout, result.stdout


def test_bare_invocation_missing_evidence_is_not_import_failure(tmp_path: Path) -> None:
    _copy_checker_fixture(tmp_path)

    result = _run_checker(tmp_path)

    assert result.returncode == 2, result.stderr
    assert "ModuleNotFoundError" not in result.stderr
    assert "缺證據檔" in result.stderr
