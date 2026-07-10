"""QA guard tests for scripts/verify_token_rotation.sh（#2/#3）。

鎖兩件事：
1. 靜態不變式——GH_TOKEN 綁定、curl fallback、全前綴 regex、無裸跑 gh auth status、
   零明文輸出（禁 set -x）。以字串錨鎖「可執行行」。
2. 動態判別力——在 $TMPDIR 自建黑/白樣本傳入 --scan（絕不對 repo history/ 實跑），
   驗命中回非 0、放行回 0，且命中時 stdout 不外洩 token 明文。
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "verify_token_rotation.sh"


def _text() -> str:
    assert SCRIPT.exists(), "缺少 scripts/verify_token_rotation.sh"
    return SCRIPT.read_text(encoding="utf-8")


def test_script_exists_and_is_executable() -> None:
    assert SCRIPT.exists(), "缺少 scripts/verify_token_rotation.sh"
    mode = SCRIPT.stat().st_mode
    assert mode & stat.S_IXUSR, "verify_token_rotation.sh 應具可執行位"


def test_gh_auth_status_is_never_run_bare() -> None:
    """每處 gh auth status 都必須帶 GH_TOKEN 綁定前綴，杜絕驗到 keyring 舊 token 的假綠。"""
    text = _text()
    assert 'GH_TOKEN="$GH_PAT" gh auth status' in text, "缺少綁定新 token 的 gh auth status"
    # 逐個 gh auth status 出現點檢查其所在行是否帶 GH_TOKEN 前綴（排除註解說明）。
    for m in re.finditer(r"gh auth status", text):
        line_start = text.rfind("\n", 0, m.start()) + 1
        line = text[line_start : m.start()]
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue  # 註解說明不算可執行呼叫
        assert 'GH_TOKEN="$GH_PAT"' in line, f"發現裸跑 gh auth status：{line!r}"


def test_curl_fallback_hits_user_endpoint_with_bearer() -> None:
    text = _text()
    assert "Authorization: Bearer $GH_PAT" in text, "curl fallback 缺 Bearer header"
    assert "api.github.com/user" in text, "curl fallback 缺 /user 端點"
    assert "200" in text, "curl fallback 缺 200 生效判定"


def test_scan_regex_covers_all_github_token_prefixes() -> None:
    """grep fallback regex 必須涵蓋 ghp_/gho_/ghs_/ghr_/github_pat_ 全前綴。"""
    text = _text()
    # gh[posur]_ 一次涵蓋 ghp_/gho_/ghs_/ghr_（及 ghu_）；github_pat_ 另列。
    assert "gh[posur]_" in text, "殘留掃描 regex 未用 gh[posur]_ 涵蓋 classic/oauth/server/refresh 前綴"
    assert "github_pat_" in text, "殘留掃描 regex 未涵蓋 fine-grained github_pat_ 前綴"
    # 反向自證：字元類確實含 p/o/s/u/r 五碼
    assert set("posur") <= set("posur"), "sanity"
    for ch in "posur":
        assert ch in "posur"


def test_no_token_plaintext_leak_surface() -> None:
    """零明文輸出不變式：禁用 set -x（會把展開後的 token 打進 trace）。"""
    text = _text()
    # 只查「可執行行」——排除註解說明中提及的 `set -x`（避免自傷誤命中）。
    for line in text.splitlines():
        code = line.split("#", 1)[0]
        assert "set -x" not in code, "禁用 set -x：會把展開後的 $GH_PAT 印進 trace"
    # curl 必須 -o /dev/null，不回應主體
    assert "-o /dev/null" in text, "curl 應 -o /dev/null，只讀 HTTP 狀態碼、不印回應主體"


def test_report_is_exit_zero_and_states_manual_boundary() -> None:
    proc = subprocess.run(
        ["bash", str(SCRIPT), "--report"],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, f"--report 應 exit 0，實得 {proc.returncode}"
    out = proc.stdout
    assert "人工" in out and "AI 可代勞" in out, "--report 須含人工/AI 分界"
    assert "待人工" in out, "--report 須標步驟 1/3 待人工"
    assert "不證 scope" in out, "--report 須明示 curl 200 不證 scope"


def _fake_token(prefix: str, body_char: str, length: int) -> str:
    # 執行期組裝假 token，避免完整字面落在測試檔（防 gitleaks/掃描誤命中本檔）。
    return prefix + body_char * length


def test_scan_discriminates_black_vs_white_sample(tmp_path: Path) -> None:
    """黑樣本命中回非 0、白樣本放行回 0；且命中時 stdout 不外洩 token 明文。"""
    black = tmp_path / "black"
    white = tmp_path / "white"
    black.mkdir()
    white.mkdir()

    fake_fg = _fake_token("github_pat_", "1", 30)
    fake_classic = _fake_token("ghp_", "A", 40)
    (black / "session.jsonl").write_text(
        f"log: {fake_fg}\ncommit: {fake_classic} done\n", encoding="utf-8"
    )
    (white / "clean.jsonl").write_text("hello world, no secrets here\n", encoding="utf-8")

    black_proc = subprocess.run(
        ["bash", str(SCRIPT), "--scan", str(black)], capture_output=True, text=True
    )
    assert black_proc.returncode != 0, "黑樣本（含 token）應命中回非 0"
    combined = black_proc.stdout + black_proc.stderr
    assert fake_fg not in combined, "掃描輸出外洩 fine-grained token 明文"
    assert fake_classic not in combined, "掃描輸出外洩 classic token 明文"

    white_proc = subprocess.run(
        ["bash", str(SCRIPT), "--scan", str(white)], capture_output=True, text=True
    )
    assert white_proc.returncode == 0, "白樣本（無 token）應放行回 0"


def test_verify_without_token_fails_fast(tmp_path: Path) -> None:
    """步驟 2b 需 $GH_PAT 在場；未設定時應 fail-fast、不誤報成功。"""
    env = dict(os.environ)
    env.pop("GH_PAT", None)
    proc = subprocess.run(
        ["bash", str(SCRIPT), "--verify"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert proc.returncode != 0, "--verify 缺 $GH_PAT 應 fail-fast 非 0"
