"""
QA 守護測試：``scripts/verify-clean.sh`` 的 WARN_FILE 警告分流契約。

職責（與既有 ``tests/test_verify_clean_acceptance.py`` 不重疊）：
  - 既有驗收測試守護「結構化輸出 + exit 反映 fail + 不偽綠」
  - 本測試守護「**警告 / stderr 寫去哪、怎麼寫、有無誤流到 stdout**」這條警告分流契約

守護的三條契約：
  (a) 宣告契約：OUT_FILE 必含一行 ``# stderr warning 檔 : <絕對路徑>`` 標頭，
      且宣告路徑與 WARN_FILE 實際落盤檔案一致（自我驗證）。
  (b) 落盤契約：宣告的 WARN_FILE 路徑在腳本跑完後**確實存在於磁碟**；
      同 TS 配對的 OUT_FILE 也必存在；兩個檔**檔名前綴**符合腳本設定
      （自我驗證——防止有人 refactor 把檔名整個換掉而測試仍綠）。
  (c) 分流契約（防呆 / 目前 vacuous truth）：
      WARN_FILE 不應含 OUT_FILE 的業務標頭字串。
      此條目前因腳本 block-level ``2> "$WARN_FILE"`` 結構自然成立
      （WARN_FILE 只接 stderr，OUT_FILE 業務標頭走 stdout 不會誤流）。
      **保留為重構時的 catch 網**——若日後有人把 block-level redirect
      拆成逐命令處理（每個 ``cat $X_ERR >> $WARN_FILE``），
      漏接一個就會被本條 catch。docstring 明確標明 vacuous 性
      避免接手者誤判守護範圍。

不守護的事項（明確聲明，避免誤判）：
  - 觸發後 stderr **內容**是否正確路由至 WARN_FILE
    （如 ``cat $X_ERR >> $WARN_FILE`` 被誤改為 ``cat $X_ERR`` 不會 catch）
  - fetch 失敗情境
  - close-out 文件撰寫
  - 假性 diff 排除政策
  上述均屬既有測試或其他任務範疇。

設計紀律：
  - 純 pytest + subprocess 黑盒；不注入 TMPDIR、不 mock。
  - 沿用既有 ``LC_ALL=C`` + ``GIT_TERMINAL_PROMPT=0`` env 慣例。
  - 正/負樣成對 parametrize 形式 (present, absent, why_present, why_absent)，
    對齊 CONTRIBUTING.md:30「守護測試必含 ≥1 個負樣斷言」規範。
  - 用 ``_evidence`` fixture 抓最新一對 OUT_FILE / WARN_FILE，
    避免重複「跑 script + glob + 配對 + 讀檔」邏輯。
  - 每次 fixture 重新跑一次 script（function scope）以保證測試隔離。

已知限制（PM/架構師定案）：
  - WARN_FILE / OUT_FILE 定位走 ``tempfile.gettempdir()`` glob（honors ``TMPDIR`` env），
    沙箱可能落 ``/tmp/claude-*``、本地可能落 ``/var/folders/*/T/``、CI 落 ``/tmp``。
    沙箱若動態切換 ``TMPDIR`` 需重跑基準化。
  - 連跑 race 用「fixture 跑前清同目錄舊檔」消解——保證 glob 抓到的
    是本次 fixture 啟動的 script 跑出來的那份。

執行：``pytest tests/qa_test_verification_warning_contract.py -v``
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# 與既有 _run() 對齊：固定 locale + 關閉 git 互動 prompt，
# 確保不同 CI 環境下字串比對穩定。
_BASE_ENV = {**os.environ, "LC_ALL": "C", "GIT_TERMINAL_PROMPT": "0"}

# 抓 WARN_FILE 宣告行的 regex。
# 腳本 OUT_FILE 標頭格式：`# stderr warning 檔 : <TMPDIR>/git-warnings-...-<TS>.log`
_DECLARE_RE = re.compile(r"^# stderr warning 檔 : (\S+)\s*$", re.MULTILINE)

# 抓檔名裡 TS（YYYYMMDDTHHMMSSZ）的 regex，用於配對 OUT_FILE 與 WARN_FILE。
_TS_RE = re.compile(r"(\d{8}T\d{6}Z)")


def _cleanup_stale_evidence_files() -> None:
    """清掉 ``tempfile.gettempdir()`` 內前次跑 verify-clean.sh 留下的舊證據檔。

    連跑 race：script 檔名是秒級 TS（`date -u +%Y%m%dT%H%M%SZ`），同一秒內
    連跑會撞名／sort by mtime 抓錯。跑前清掉同目錄的舊 ``git-warnings-*.log``
    與 ``clean-verify-output-*.txt`` 消解 race。

    範圍嚴格限定本測試「負責」的那兩個檔名前綴，不會誤刪其他 process 的檔。
    """
    tmp_dir = Path(tempfile.gettempdir())
    for pattern in ("git-warnings-*.log", "clean-verify-output-*.txt"):
        for stale in tmp_dir.glob(pattern):
            try:
                stale.unlink()
            except FileNotFoundError:
                pass  # 其他 process 同時在清，race-safe


# --- fixture ---------------------------------------------------------------


@pytest.fixture
def _evidence() -> Iterator[tuple[Path, Path, str]]:
    """跑一次 verify-clean.sh，回傳 (out_file, warn_file, stdout_text) 三元組。

    - out_file / warn_file：配對的證據檔（同一 TS）
    - stdout_text：subprocess 捕獲到的 stdout（黑盒觀察用）

    跑前先清掉同目錄內的舊證據檔，確保 glob 抓到的是本次 fixture 啟動的
    script 跑出來的那一份（消解秒級 TS 連跑 race）。
    """
    _cleanup_stale_evidence_files()

    proc = subprocess.run(
        ["bash", str(REPO_ROOT / "scripts" / "verify-clean.sh")],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        env=_BASE_ENV,
    )

    # 腳本內部 block-level redirect 把 stdout 全導去 OUT_FILE，
    # 故外部 capture 為空是預期內；我們改讀 OUT_FILE 取得實際 stdout 內容。
    # 兩個檔同 TS 配對（腳本內 `TS="$(date -u +...)"` 一次取值）。
    # glob 基準用 `tempfile.gettempdir()` honors `TMPDIR` env，沙箱可能落
    # `/tmp/claude-*`，寫死 `/tmp` 會 0 命中。
    tmp_dir = Path(tempfile.gettempdir())

    warn_files = sorted(
        tmp_dir.glob("git-warnings-*.log"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    assert warn_files, f"腳本跑完後 {tmp_dir}/git-warnings-*.log 不存在（落盤契約直接破）"
    warn_file = warn_files[0]

    ts_match = _TS_RE.search(warn_file.name)
    assert ts_match, f"WARN_FILE 檔名缺 TS 段: {warn_file.name}"
    ts = ts_match.group(1)

    out_candidates = sorted(
        tmp_dir.glob(f"clean-verify-output-*-{ts}.txt"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    assert out_candidates, f"找不到配對的 OUT_FILE（TS={ts}）"
    out_file = out_candidates[0]

    yield out_file, warn_file, proc.stdout


# --- 契約 (a)：宣告契約 -----------------------------------------------------


def test_warn_file_declared_in_stdout(_evidence: tuple[Path, Path, str]) -> None:
    """OUT_FILE 必含 ``# stderr warning 檔 : <絕對路徑>`` 宣告行。

    必對應的副斷言：宣告路徑必指向 fixture 抓到的 WARN_FILE 實體
    （自我驗證——確保宣告與實作未漂移）。
    """
    out_file, warn_file, _ = _evidence
    out_text = out_file.read_text(encoding="utf-8", errors="replace")

    match = _DECLARE_RE.search(out_text)
    assert match, (
        f"OUT_FILE 缺『# stderr warning 檔 : <path>』宣告行\n"
        f"OUT_FILE 前 15 行：\n{chr(10).join(out_text.splitlines()[:15])}"
    )
    declared_path = Path(match.group(1))
    assert declared_path == warn_file, (
        f"宣告路徑與 fixture 抓到的 WARN_FILE 不一致：\n  宣告：{declared_path}\n  實體：{warn_file}"
    )


# --- 契約 (b)：落盤契約 -----------------------------------------------------


@pytest.mark.parametrize(
    "filename,expected_prefix,why",
    [
        pytest.param(
            "warn",
            "git-warnings-",
            "WARN_FILE 檔名應以 git-warnings- 前綴（防止 refactor 改檔名而測試仍綠）",
            id="warn-file-prefix",
        ),
        pytest.param(
            "out",
            "clean-verify-output-",
            "OUT_FILE 檔名應以 clean-verify-output- 前綴（同上理由）",
            id="out-file-prefix",
        ),
    ],
)
def test_evidence_files_have_expected_name_prefix(
    _evidence: tuple[Path, Path, str],
    filename: str,
    expected_prefix: str,
    why: str,
) -> None:
    """WARN_FILE 與 OUT_FILE 必存在於磁碟，且檔名前綴符合腳本設定。

    形式：每個 case 同時含「必含」正向斷言（檔名以 prefix 開頭）
    與「必不含」負向斷言（檔名不應是空字串——防 fixture 退化）。
    """
    out_file, warn_file, _ = _evidence
    target = warn_file if filename == "warn" else out_file

    assert target.exists(), f"{filename}_file 不存在於磁碟：{target}"

    target_name = target.name
    assert target_name.startswith(expected_prefix), (
        f"{filename}_file 檔名前綴不符（{why}）：實際={target_name!r} 期望前綴={expected_prefix!r}"
    )
    # 負樣斷言：檔名不應為空字串（防 fixture 回傳空路徑的退化情境）
    assert target_name != expected_prefix, (
        f"{filename}_file 檔名僅剩前綴，缺 branch/TS 段：{target_name!r}"
    )


def test_out_and_warn_files_are_distinct(_evidence: tuple[Path, Path, str]) -> None:
    """OUT_FILE 與 WARN_FILE 必指向不同檔（兩檔職責分離）。"""
    out_file, warn_file, _ = _evidence
    assert out_file != warn_file, f"OUT_FILE 與 WARN_FILE 指向同一檔（職責未分離）：{out_file}"
    # 負樣：任一檔不得為對方的絕對路徑子字串以外的東西（即：兩者必須各自存在）
    assert out_file.is_file() and warn_file.is_file(), (
        f"OUT_FILE 或 WARN_FILE 不是 regular file：out={out_file} warn={warn_file}"
    )


# --- 契約 (c)：分流契約（防呆 / 目前 vacuous truth）-------------------------


@pytest.mark.parametrize(
    "absent_substr,why_absent",
    [
        pytest.param(
            "# verify-clean.sh 結構化輸出",
            "OUT_FILE 業務標頭不應誤流到 WARN_FILE（分流契約：stderr 與 stdout 檔分離）",
            id="out-header-leak",
        ),
        pytest.param(
            "## Step ",
            "OUT_FILE 的 Step 區段標題不應誤流到 WARN_FILE（分流契約：業務分段不污染警告檔）",
            id="step-header-leak",
        ),
    ],
)
def test_warn_file_does_not_contain_stdout_business_headers(
    _evidence: tuple[Path, Path, str],
    absent_substr: str,
    why_absent: str,
) -> None:
    """WARN_FILE 不應含 OUT_FILE 業務標頭字串。

    **目前為 vacuous truth**：腳本用 block-level ``{ ... } > OUT 2> WARN``
    結構，WARN_FILE 只接 stderr，業務標頭走 stdout 不會誤流。

    保留為**重構時的 catch 網**：若日後有人拆成逐命令處理
    （如每個 ``cat $X_ERR >> WARN_FILE`` 漏接），此條會 fail。
    接手者請勿在「測試全綠就刪掉」的念頭下移除——這條的價值在
    「重構觸發時第一時間 fail」。

    形式上以「單 absent 樣」呈現（非正/負樣成對），
    因為這條契約本身只有「負樣」語意——「WARN_FILE 不該含什麼」，
    沒有對應的「WARN_FILE 該含什麼」正向契約（具體 stderr 內容
    屬觸發情境，不在本守護範圍）。
    """
    _, warn_file, _ = _evidence
    warn_text = warn_file.read_text(encoding="utf-8", errors="replace")

    assert absent_substr not in warn_text, (
        f"WARN_FILE 誤含『{absent_substr}』（{why_absent}）\nWARN_FILE 內容：\n{warn_text[:500]}"
    )
