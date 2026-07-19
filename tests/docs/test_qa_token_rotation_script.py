"""QA guard tests for scripts/verify_token_rotation.sh.

鎖定 GH_TOKEN 綁定、curl fallback、全前綴掃描 regex、零明文輸出，以及
黑/白樣本對 grep fallback 的真實判別力。黑/白樣本只放在 pytest tmpdir，
不落 repo history/ 或 workspace。
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "verify_token_rotation.sh"


def _script_text() -> str:
    assert SCRIPT.exists(), "缺少 scripts/verify_token_rotation.sh"
    return SCRIPT.read_text(encoding="utf-8")


def _script_token_re() -> re.Pattern[str]:
    match = re.search(r"^TOKEN_RE='([^']+)'$", _script_text(), re.MULTILINE)
    assert match, "腳本缺少單一 TOKEN_RE 定義"
    return re.compile(match.group(1))


def _executable_shell_lines(text: str) -> list[tuple[int, str]]:
    lines: list[tuple[int, str]] = []
    heredoc_end: str | None = None

    for line_number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if heredoc_end is not None:
            if stripped == heredoc_end:
                heredoc_end = None
            continue

        heredoc = re.search(r"<<-?\s*'?([A-Za-z0-9_]+)'?", line)
        if heredoc:
            heredoc_end = heredoc.group(1)

        code = line.split("#", 1)[0].strip()
        if code:
            lines.append((line_number, code))

    return lines


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


def test_gh_auth_status_is_never_run_bare() -> None:
    text = _script_text()
    assert 'GH_TOKEN="$GH_PAT" gh auth status' in text

    for line_number, line in _executable_shell_lines(text):
        if "gh auth status" not in line:
            continue
        assert (
            'GH_TOKEN="$GH_PAT"' in line
        ), f"第 {line_number} 行可執行路徑出現未明確綁 GH_TOKEN 的 gh auth status"


def test_curl_fallback_uses_user_endpoint_without_response_body() -> None:
    text = _script_text()

    assert "https://api.github.com/user" in text
    assert "curl" in text
    assert "-o /dev/null" in text
    assert "-H @-" in text, "curl header 應由 stdin 傳入，避免 token 出現在 argv"
    assert "200" in text


def test_scan_regex_covers_all_github_token_prefixes() -> None:
    text = _script_text()
    token_re = _script_token_re()

    assert "gh[posur]_" in text
    assert "github_pat_" in text
    for prefix in ["ghp_", "github_pat_", "gho_", "ghs_", "ghr_"]:
        token = _fake_token(prefix, "A", 40 if prefix != "github_pat_" else 24)
        assert token_re.search(token), f"TOKEN_RE 未涵蓋 {prefix}"


def test_no_token_plaintext_leak_surface() -> None:
    text = _script_text()

    for line in text.splitlines():
        code = line.split("#", 1)[0]
        assert "set -x" not in code, "禁用 shell xtrace，避免展開後的 token 進入輸出"
    assert "--redact=100" not in text, "gitleaks redact 不鎖特定新版語法"
    assert "sed -E" not in text, "grep fallback 遮蔽不得依賴 sed"


def test_verify_uses_gh_token_from_gh_pat_without_printing_value(tmp_path: Path) -> None:
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    (fakebin / "dirname").symlink_to("/usr/bin/dirname")
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
        env={"PATH": f"{fakebin}:{os.environ['PATH']}", "GH_PAT": sentinel},
    )

    combined = proc.stdout + proc.stderr
    assert proc.returncode == 0, combined
    assert sentinel not in combined, "verify 不得輸出 GH_PAT 值"


def test_verify_curl_fallback_requires_http_200_without_printing_value(tmp_path: Path) -> None:
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    (fakebin / "dirname").symlink_to("/usr/bin/dirname")
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
    assert "curl /user HTTP: 200" in combined
    assert "不證 scope" in combined
    assert sentinel not in combined, "curl fallback 不得輸出 GH_PAT 值"


def _fake_token(prefix: str, body_char: str, length: int) -> str:
    return prefix + body_char * length


def test_scan_grep_fallback_flags_black_samples_and_hides_values(tmp_path: Path) -> None:
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    _write_executable(
        fakebin / "gitleaks",
        """#!/bin/sh
if [ "$1" = "detect" ] && [ "$2" = "--help" ]; then
  echo "fake gitleaks help without redact"
  exit 0
fi
exit 99
""",
    )

    black_dir = tmp_path / "black"
    black_dir.mkdir()
    black_tokens = [
        _fake_token("ghp_", "A", 36),
        _fake_token("gho_", "B", 36),
        _fake_token("ghs_", "C", 36),
        _fake_token("ghr_", "D", 36),
        _fake_token("github_pat_", "E", 20),
    ]
    (black_dir / "sample.txt").write_text("\n".join(black_tokens), encoding="utf-8")

    proc = _run_script(
        "--scan",
        str(black_dir),
        env={"PATH": f"{fakebin}:{os.environ['PATH']}"},
    )

    combined = proc.stdout + proc.stderr
    assert proc.returncode == 2, combined
    assert "grep fallback" in combined
    assert "疑似殘留 token" in combined
    for token in black_tokens:
        assert token not in combined, "掃描輸出不得吐出 token 明文"


def test_scan_grep_fallback_allows_white_samples(tmp_path: Path) -> None:
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    _write_executable(
        fakebin / "gitleaks",
        """#!/bin/sh
if [ "$1" = "detect" ] && [ "$2" = "--help" ]; then
  echo "fake gitleaks help without redact"
  exit 0
fi
exit 99
""",
    )

    white_dir = tmp_path / "white"
    white_dir.mkdir()
    white_samples = [
        _fake_token("ghp_", "A", 35),
        _fake_token("gho_", "B", 35),
        _fake_token("ghs_", "C", 35),
        _fake_token("ghr_", "D", 35),
        _fake_token("github_pat_", "E", 19),
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
    assert "grep fallback 未發現殘留 token" in combined


def test_report_states_manual_ai_boundary_and_scope_warning() -> None:
    proc = _run_script("--report")
    combined = proc.stdout + proc.stderr

    assert proc.returncode == 0, combined
    assert "人工" in combined and "AI 可代勞" in combined
    assert "步驟 1（發新）與步驟 3（撤舊）待人工於 GitHub UI 完成" in combined
    assert 'GH_TOKEN="$GH_PAT" gh auth status' in combined
    assert "200 只證身分有效、不證 scope" in combined
    assert "四項規格" in combined


def test_verify_without_token_fails_fast(tmp_path: Path) -> None:
    env = dict(os.environ)
    env.pop("GH_PAT", None)
    env["TOKEN_ROTATION_ENV_FILE"] = str(tmp_path / "missing.env")

    proc = _run_script("--verify", env=env)

    combined = proc.stdout + proc.stderr
    assert proc.returncode != 0, combined
    assert "未設定 $GH_PAT" in combined
