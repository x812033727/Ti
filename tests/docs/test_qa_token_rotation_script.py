"""QA guard tests for the GH_PAT token rotation helper script."""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "verify_token_rotation.sh"


def _script_text() -> str:
    assert SCRIPT.exists(), "缺少 scripts/verify_token_rotation.sh"
    return SCRIPT.read_text(encoding="utf-8")


def _run_script(
    *args: str,
    env: dict[str, str] | None = None,
    timeout: int = 20,
) -> subprocess.CompletedProcess[str]:
    run_env = os.environ.copy()
    if env:
        run_env.update(env)
    return subprocess.run(
        ["/bin/bash", str(SCRIPT), *args],
        cwd=ROOT,
        env=run_env,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def test_script_exists_and_is_executable() -> None:
    assert SCRIPT.exists(), "缺少 scripts/verify_token_rotation.sh"
    assert os.access(SCRIPT, os.X_OK), "verify_token_rotation.sh 必須可執行"


def test_static_guards_lock_gh_token_binding_curl_fallback_and_prefixes() -> None:
    text = _script_text()

    assert 'GH_TOKEN="$GH_PAT" gh auth status' in text
    assert "https://api.github.com/user" in text
    assert "curl" in text
    assert "200" in text
    assert "set -x" not in text, "腳本不得啟用 shell xtrace，避免 token 被展開輸出"
    assert "--redact=100" not in text, "gitleaks redact 需用相容舊版的 --redact"
    assert "sed -E" not in text, "grep fallback 遮蔽不得依賴 sed"

    for prefix in ["ghp_", "github_pat_", "gho_", "ghs_", "ghr_"]:
        assert prefix in text, f"grep fallback regex 未涵蓋 {prefix}"

    for line_number, line in enumerate(text.splitlines(), start=1):
        if "gh auth status" in line:
            assert 'GH_TOKEN="$GH_PAT"' in line, (
                f"第 {line_number} 行出現未明確綁 GH_TOKEN 的 gh auth status"
            )


def test_verify_uses_gh_token_from_gh_pat_without_printing_value(tmp_path: Path) -> None:
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    (fakebin / "dirname").symlink_to("/bin/dirname")
    _write_executable(
        fakebin / "gh",
        """#!/bin/sh
if [ "$1" != "auth" ] || [ "$2" != "status" ]; then
  echo "unexpected gh args" >&2
  exit 98
fi
if [ -z "${GH_TOKEN:-}" ] || [ "${GH_TOKEN:-}" != "${GH_PAT:-}" ]; then
  echo "GH_TOKEN was not bound from GH_PAT" >&2
  exit 97
fi
echo "fake gh status ok"
""",
    )

    sentinel = "sentinel-value-not-a-token"
    proc = _run_script(
        "--verify",
        env={"PATH": str(fakebin), "GH_PAT": sentinel},
    )

    combined = proc.stdout + proc.stderr
    assert proc.returncode == 0, combined
    assert "fake gh status ok" in combined
    assert sentinel not in combined, "verify 不得輸出 GH_PAT 值"


def test_verify_curl_fallback_requires_http_200_without_printing_value(tmp_path: Path) -> None:
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    (fakebin / "dirname").symlink_to("/bin/dirname")
    _write_executable(
        fakebin / "curl",
        """#!/bin/sh
while IFS= read -r _line; do
  :
done
printf '200'
""",
    )

    sentinel = "another-sentinel-value"
    proc = _run_script(
        "--verify",
        env={"PATH": str(fakebin), "GH_PAT": sentinel},
    )

    combined = proc.stdout + proc.stderr
    assert proc.returncode == 0, combined
    assert "curl fallback" in combined
    assert "HTTP 200" in combined
    assert "scope" in combined
    assert sentinel not in combined, "curl fallback 不得輸出 GH_PAT 值"


def test_scan_grep_fallback_flags_black_samples_and_redacts_values(tmp_path: Path) -> None:
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    _write_executable(
        fakebin / "gitleaks",
        """#!/bin/sh
if [ "$1" = "detect" ] && [ "$2" = "--help" ]; then
  echo "fake gitleaks help"
  exit 0
fi
echo "fake gitleaks scan path should not run" >&2
exit 99
""",
    )

    black_dir = tmp_path / "black"
    black_dir.mkdir()
    black_tokens = [
        "ghp_" + "A" * 36,
        "gho_" + "B" * 36,
        "ghs_" + "C" * 36,
        "ghr_" + "D" * 36,
        "github_pat_" + "E" * 20,
    ]
    (black_dir / "sample.txt").write_text("\n".join(black_tokens), encoding="utf-8")

    proc = _run_script(
        "--scan",
        str(black_dir),
        env={"PATH": f"{fakebin}:{os.environ['PATH']}"},
    )

    combined = proc.stdout + proc.stderr
    assert proc.returncode == 1, combined
    assert "using grep fallback" in combined
    assert combined.count("[REDACTED_GITHUB_TOKEN]") >= len(black_tokens)
    for token in black_tokens:
        assert token not in combined, "掃描輸出不得吐出 token 明文"


def test_scan_grep_fallback_allows_white_samples(tmp_path: Path) -> None:
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    _write_executable(
        fakebin / "gitleaks",
        """#!/bin/sh
if [ "$1" = "detect" ] && [ "$2" = "--help" ]; then
  echo "fake gitleaks help"
  exit 0
fi
exit 99
""",
    )

    white_dir = tmp_path / "white"
    white_dir.mkdir()
    white_samples = [
        "ghp_" + "A" * 35,
        "gho_" + "B" * 35,
        "ghs_" + "C" * 35,
        "ghr_" + "D" * 35,
        "github_pat_" + "E" * 19,
        "not-a-token",
    ]
    (white_dir / "sample.txt").write_text("\n".join(white_samples), encoding="utf-8")

    proc = _run_script(
        "--scan",
        str(white_dir),
        env={"PATH": f"{fakebin}:{os.environ['PATH']}"},
    )

    combined = proc.stdout + proc.stderr
    assert proc.returncode == 0, combined
    assert "PASS: grep fallback found no residual GitHub token" in combined


def test_report_states_manual_ai_boundary_and_scope_warning() -> None:
    proc = _run_script("--report")
    combined = proc.stdout + proc.stderr

    assert proc.returncode == 0, combined
    assert "| 步驟 | 誰做 | 狀態 | 分界 |" in combined
    assert "1. 發新 fine-grained PAT | 人工 | 待人工" in combined
    assert "3. 撤銷舊 token | 人工 | 待人工" in combined
    assert "AI 可代勞" in combined
    assert 'GH_TOKEN="$GH_PAT" gh auth status' in combined
    assert "curl HTTP 200 只證明身分有效，不證 repository scope" in combined
    assert "四項 GH_PAT 規格" in combined
