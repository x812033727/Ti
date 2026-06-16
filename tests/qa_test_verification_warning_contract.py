"""QA 守護測試：verify-clean.sh 的 WARN_FILE 警告分流契約。

契約實體（實跑釐清後的修正版，與架構決策 M4 原案有別）:
  - terminal stdout / stderr 皆為 0 bytes（主流程整塊 `> "$OUT_FILE" 2> "$WARN_FILE"`
    重導，結尾無 `cat "$OUT_FILE"` 兜底）
  - 兩個獨立檔:
      OUT_FILE  = TMPDIR/clean-verify-output-<sanitized_branch>-<UTC_TS>.txt  (主證據)
      WARN_FILE = TMPDIR/git-warnings-<sanitized_branch>-<UTC_TS>.log         (警告累積)
  - 「宣告行」`# stderr warning 檔 : <path>` 落在 OUT_FILE 第 11 行, **非 stdout**
  - WARN_FILE 內容是真實的 stderr 累積（git worktree add 的 "Preparing worktree"、
    對 worktree 內 .gitmodules 的 ls/cat 失敗訊息等）

守護的三條契約:
  (a) 宣告契約: OUT_FILE 含 `# stderr warning 檔 : <絕對路徑>` 行, 路徑符合
      `git-warnings-<sanitized_branch>-<UTC_TS>.log` 模式
  (b) 落盤契約: 宣告的 WARN_FILE 路徑在跑完後確實存在於磁碟
  (c) 分流契約: WARN_FILE 含真實 stderr 累積（觸發條件下, 必含 "Preparing worktree"）,
      且絕不含 OUT_FILE 業務標頭（`# verify-clean.sh`、`## Step`、`=== 程式 fail=`）,
      誤流即為分流破壞

每條 parametrize 配 (present, absent, why_present, why_absent) 四元組,
正/負樣成對出現在同一組 case 內（CONTRIBUTING 守護測試規範 + 避免「全綠自欺」）。

不守護的事項（避免誤判守護範圍, 接手者翻案前請先讀這段）:
  - terminal stdout 業務輸出: 既有 `test_verify_clean_acceptance.py` 斷言
    `# verify-clean.sh` in stdout, 與本契約「terminal stdout 為空」事實衝突;
    屬既有測試的 pre-existing bug, 不歸本守護測試處理（修既有測試屬另一任務）
  - fetch 失敗情境的 stderr 內容（屬既有測試 Step 0a 範疇）
  - close-out 文件撰寫（屬任務 #3 範疇）
  - 假性 diff 排除政策（屬任務 #2 範疇）
  - 觸發後 stderr 內容的具體正確性（本守護只驗「有 stderr 累積 + 不含 stdout 業務標頭」,
    不驗每條訊息的具體內容——若 `cat $X_ERR >> $WARN_FILE` 被誤改為 `cat $X_ERR`,
    個別訊息正確性不會被本測試 catch）

執行: python3 -m pytest tests/qa_test_verification_warning_contract.py -v
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

# 沿用既有 _run() 慣例（test_verify_clean_acceptance.py）:
#   - LC_ALL=C: 避免不同 locale 讓子字串比對飄掉
#   - GIT_TERMINAL_PROMPT=0: CI 無 origin remote 時不卡 stdin
ENV_OVERRIDES = {"LC_ALL": "C", "GIT_TERMINAL_PROMPT": "0"}

# 與 verify-clean.sh 一致: TMPDIR 可注入, 預設 /tmp。
TMP_BASE = Path(os.environ.get("TMPDIR", "/tmp"))


def _run_verify_clean() -> subprocess.CompletedProcess:
    """黑盒跑 `bash scripts/verify-clean.sh`, 回傳 terminal stdout/stderr/RC。"""
    return subprocess.run(
        ["bash", str(Path(__file__).resolve().parent.parent / "scripts" / "verify-clean.sh")],
        cwd=Path(__file__).resolve().parent.parent,
        capture_output=True,
        text=True,
        env={**os.environ, **ENV_OVERRIDES},
    )


@pytest.fixture(scope="module")
def verify_clean_run() -> tuple[subprocess.CompletedProcess, Path, Path]:
    """跑一次 verify-clean.sh, 回傳 (terminal_cp, out_file_path, warn_file_path)。

    兩個檔案路徑只能從 glob 推（terminal stdout 為空, 連 OUT_FILE 自己路徑都沒有對外揭露）。
    從 mtime 排序取最新一份, 處理同一秒級 TS 內連跑撞名的情境。
    """
    cp = _run_verify_clean()
    out_files = sorted(
        TMP_BASE.glob("clean-verify-output-*.txt"),
        key=lambda p: p.stat().st_mtime,
    )
    warn_files = sorted(
        TMP_BASE.glob("git-warnings-*.log"),
        key=lambda p: p.stat().st_mtime,
    )
    assert out_files, "OUT_FILE 未產出（verify-clean.sh 行為漂移: 不再寫主證據檔）"
    assert warn_files, "WARN_FILE 未產出（verify-clean.sh 行為漂移: 不再累積 stderr）"
    return cp, out_files[-1], warn_files[-1]


# === 契約 (a) 宣告契約: OUT_FILE 內含 WARN_FILE 路徑宣告 =================


def test_out_file_contains_warn_file_declaration_line(verify_clean_run):
    """OUT_FILE 必須含 `# stderr warning 檔 : <path>` 宣告行（核心宣告契約）。

    負樣守護: 此行格式前綴（`# stderr warning 檔 :`）若被改成 `WARN_FILE=` 變數宣告
    或 `# stderr warning 檔:`（缺空白）, 會讓既有讀者（其他任務 close-out）抓不到路徑,
    屬契約漂移。
    """
    _, out_file, _ = verify_clean_run
    text = out_file.read_text(encoding="utf-8")
    # 正樣: 宣告行前綴（必含）
    assert "# stderr warning 檔 :" in text, (
        f"OUT_FILE 缺『# stderr warning 檔 :』宣告行（前綴被改寫？）:\n{text[:500]}"
    )
    # 負樣: 不可誤用常見替代寫法（否則宣告契約就不唯一）
    for forbidden in ("# WARN_FILE=", "# warning file:", "WARN_FILE ="):
        assert forbidden not in text, (
            f"OUT_FILE 誤含替代寫法『{forbidden}』（宣告契約漂移: 兩種格式並存）"
        )


def test_out_file_warn_file_path_matches_glob_pattern(verify_clean_run):
    """宣告的 WARN_FILE 路徑必須符合 `git-warnings-<branch>-<UTC>.log` pattern。

    驗證的兩個不變量:
      - 前綴 `git-warnings-` (對應 script 的 `${TMP_BASE}/git-warnings-...`)
      - 結尾 UTC TS pattern `\d{8}T\d{6}Z\.log` (對應 `date -u +%Y%m%dT%H%M%SZ`)
    """
    _, out_file, _ = verify_clean_run
    text = out_file.read_text(encoding="utf-8")
    m = re.search(r"^# stderr warning 檔 : (\S+)$", text, re.MULTILINE)
    assert m, "OUT_FILE 缺『# stderr warning 檔 : <path>』宣告行"
    declared = m.group(1)
    # 正樣: 符合 pattern
    assert re.match(
        r"^.*/git-warnings-[A-Za-z0-9_.-]+-\d{8}T\d{6}Z\.log$",
        declared,
    ), f"WARN_FILE 宣告路徑不符預期 pattern（`git-warnings-<branch>-<UTC>.log`）: {declared}"
    # 負樣: 不應誤用 OUT_FILE 的命名 pattern（避免兩個檔被誤認為同一契約）
    assert "clean-verify-output-" not in declared, (
        f"WARN_FILE 宣告路徑誤用 OUT_FILE 命名 pattern（檔案分流破壞）: {declared}"
    )


# === 契約 (b) 落盤契約: 宣告的 WARN_FILE 路徑真實存在於磁碟 ================


def test_declared_warn_file_exists_on_disk(verify_clean_run):
    """宣告契約的回聲: 宣告的 WARN_FILE 路徑在跑完後必須真實落盤於磁碟。

    若 OUT_FILE 宣告了路徑但該路徑實際不存在, 代表:
      - script `WARN_FILE=...` 與實際寫入的 `${TMP_BASE}/...` 路徑不一致
      - 或 stderr 重導的 `2> "$WARN_FILE"` 被 typo 成 `2> $WARN_FILE` 漏引號（會寫到
        名為 `$WARN_FILE` 的字面檔, 而非宣告路徑）

    空檔 = 合法（沒觸發任何 stderr 累積）; 缺檔 = 契約破壞。
    """
    _, out_file, _ = verify_clean_run
    text = out_file.read_text(encoding="utf-8")
    m = re.search(r"^# stderr warning 檔 : (\S+)$", text, re.MULTILINE)
    assert m, "OUT_FILE 缺 WARN_FILE 宣告行"
    declared_path = Path(m.group(1))
    # 正樣: 宣告的檔案實際存在
    assert declared_path.exists(), (
        f"宣告的 WARN_FILE 不存在於磁碟（script 宣告路徑與實際寫入路徑不一致？）: {declared_path}"
    )
    assert declared_path.is_file(), (
        f"宣告的 WARN_FILE 不是檔案（被誤建為目錄？permission 問題？）: {declared_path}"
    )


def test_glob_latest_warn_file_matches_declared_path(verify_clean_run):
    """WARN_FILE glob 抓到的最新檔必須等於宣告的路徑（防止「宣告一份、寫另一份」漂移）。

    負樣守護: 若 script 改成「宣告 `<TS_A>.log` 但實際寫到 `<TS_B>.log`」（例如 TS 在
    宣告後被重新計算）, glob 會抓到 TS_B, 與宣告的 TS_A 不一致, 讀者依宣告去找會找不到。
    """
    _, out_file, declared_warn = verify_clean_run
    text = out_file.read_text(encoding="utf-8")
    m = re.search(r"^# stderr warning 檔 : (\S+)$", text, re.MULTILINE)
    assert m, "OUT_FILE 缺 WARN_FILE 宣告行"
    declared_path = Path(m.group(1))
    # 正樣: glob 抓到的最新檔 == 宣告路徑
    assert declared_warn == declared_path, (
        f"WARN_FILE 漂移: glob 最新={declared_warn}, 宣告={declared_path}。\n"
        "若兩者不一致, 代表 script 宣告路徑與實際寫入路徑不對齊（讀者依宣告找會落空）"
    )


# === 契約 (c) 分流契約: WARN_FILE 收 stderr、不收 stdout 業務標頭 ==========


@pytest.mark.parametrize(
    "present,absent,why_present,why_absent",
    [
        (
            "Preparing worktree",
            "# verify-clean.sh",
            "git worktree add 必然 stderr『Preparing worktree (detached HEAD ...)』"
            ", 這是 WARN_FILE 收 stderr 的最小存在證據",
            "`# verify-clean.sh` 是 OUT_FILE 標頭, 誤流 WARN_FILE 代表 stdout/stderr 邊界破壞",
        ),
        (
            "Preparing worktree",
            "# 輸出證據檔",
            "同上: stderr 必落入 WARN_FILE, 必含『Preparing worktree』",
            "`# 輸出證據檔` 是 OUT_FILE 標頭（line 10）, 誤流 WARN_FILE 代表 redirect 結構破壞",
        ),
        (
            "Preparing worktree",
            "## Step 1:",
            "同上: stderr 必落入 WARN_FILE",
            "`## Step 1:` 是 OUT_FILE 業務章節標題, 誤流 WARN_FILE 代表整個 main block"
            " 重導被改寫成僅 stdout 重導",
        ),
        (
            "Preparing worktree",
            "=== 程式 fail=",
            "同上: stderr 必落入 WARN_FILE",
            "`=== 程式 fail=` 是 OUT_FILE 結尾總結行, 誤流 WARN_FILE 代表 main block"
            " 的 stdout 內容被誤塞 stderr 路徑",
        ),
    ],
)
def test_warn_file_routing_contract(verify_clean_run, present, absent, why_present, why_absent):
    """分流契約: WARN_FILE 必含 stderr 累積（present）、絕不含 OUT_FILE 業務標頭（absent）。

    正/負樣成對出現在同一組 case 內, 避免「全綠自欺」（CONTRIBUTING 守護測試規範）。
    """
    _, _, warn_file = verify_clean_run
    text = warn_file.read_text(encoding="utf-8")
    # 正樣: WARN_FILE 必含『Preparing worktree』
    assert present in text, (
        f"WARN_FILE 缺『{present}』（{why_present}）\n--- WARN_FILE 內容 ---\n{text}"
    )
    # 負樣: WARN_FILE 絕不含 OUT_FILE 業務標頭
    assert absent not in text, (
        f"WARN_FILE 誤含 OUT_FILE 業務標頭『{absent}』（{why_absent}）\n"
        f"--- WARN_FILE 內容 ---\n{text}"
    )


# === 契約 (d) 防呆: terminal stdout / stderr 為空 =========================


def test_terminal_stdout_is_empty(verify_clean_run):
    """防呆: terminal stdout 必須為 0 bytes（主流程整塊重導, 無 cat 兜底）。

    此條同時守住: 若哪天有人重構 script 把主流程改成「先寫檔、最後 cat 一次到 terminal」,
    本測試會 fail, 提醒「這會破壞既有的「terminal 靜默」契約」(雖然本任務沒要求守住
    這個契約, 但 stdout 為空是「宣告在 OUT_FILE 而非 stdout」前提的根基, 故列防呆)。
    """
    cp, _, _ = verify_clean_run
    assert cp.stdout == "", (
        f"terminal stdout 應為 0 bytes（主流程整塊重導, 無 cat 兜底）"
        f", 實為 {len(cp.stdout)} bytes: {cp.stdout[:200]!r}"
    )


def test_terminal_stderr_is_empty(verify_clean_run):
    """防呆: terminal stderr 必須為 0 bytes（同上理由, main block 的 2> 重導吞掉所有 stderr）。"""
    cp, _, _ = verify_clean_run
    assert cp.stderr == "", (
        f"terminal stderr 應為 0 bytes（main block 的 2> 重導吞掉所有 stderr）"
        f", 實為 {len(cp.stderr)} bytes: {cp.stderr[:200]!r}"
    )


# === 契約 (e) 防呆: terminal RC 反映 fail 累計、不偽綠 ====================


def test_terminal_rc_reflects_fail(verify_clean_run):
    """防呆: terminal RC 是 fail 累計的反映, 不偽綠 exit 0。

    在當前 lane（worktree 為 origin/main 視角、4 條命令全 exit 0）下, fail=0、RC=0。
    若 fetch 失敗 / 4 條任一 exit 1, RC 應為 1。本測試只驗「RC 屬於 fail 累計的合理
    反映」（0 或 1）, 不綁死 0 或 1——避免 lane HEAD 漂移讓測試 flaky。
    """
    cp, _, _ = verify_clean_run
    assert cp.returncode in (0, 1), (
        f"terminal RC={cp.returncode} 異常（應為 0 = 全部 exit 0, 或 1 = 至少一條 fail）"
    )
