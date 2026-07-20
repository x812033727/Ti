"""
QA 守護測試：``scripts/verify-clean.sh`` 的 WARN_FILE 警告分流契約。

職責（與既有 ``tests/test_verify_clean_acceptance.py`` 不重疊）：
  - 既有驗收測試守護「結構化輸出 + exit 反映 fail + 不偽綠」
  - 本測試守護「**警告 / stderr 寫去哪、怎麼寫、有無誤流到 stdout**」這條警告分流契約

守護的三條契約：
  (a) 宣告契約：OUT_FILE 必含一行 ``# stderr warning 檔 : <絕對路徑>`` 標頭，
      且宣告路徑與 WARN_FILE 實際落盤檔案一致（自我驗證）。
  (b) 落盤契約：宣告的 WARN_FILE 路徑在腳本跑完後**確實存在於磁碟**；
      同輪 OUT_FILE 由 stdout 宣告的每輪 manifest 指定；
      兩個檔**檔名前綴**符合腳本設定（自我驗證——防止有人 refactor
      把檔名整個換掉而測試仍綠）。
  (c) 分流契約（防呆 / 目前 vacuous truth，但 parametrize 採正/負樣成對）：
      WARN_FILE 必含真實 stderr 累積（正樣：『Preparing worktree』），
      絕不含 OUT_FILE 業務標頭（負樣：`# verify-clean.sh 結構化輸出`、
      `## Step `）。此條目前因腳本 block-level ``2> "$WARN_FILE"`` 結構
      自然成立——stderr 走 WARN_FILE、stdout 業務標頭走 OUT_FILE，
      兩者本來就不交會。
      **保留為重構時的 catch 網**——若日後有人把 block-level redirect
      拆成逐命令處理（每個 ``cat $X_ERR >> $WARN_FILE``），
      漏接一個就會被本條 catch。docstring 明確標明 vacuous 性
      避免接手者誤判守護範圍。

不守護的事項（明確聲明，避免誤判）：
  - 觸發後 stderr **內容**是否正確路由至 WARN_FILE
    （如 ``cat $X_ERR >> $WARN_FILE`` 被誤改為 ``cat $X_ERR`` 不會 catch）
  - terminal stdout 業務標頭：腳本 block-level redirect 把腳本本身
    ``echo`` 出去的 stdout 也帶進 OUT_FILE，外部 ``subprocess.run`` capture
    的 stdout 為 0 bytes 是事實。既有 ``test_verify_clean_acceptance.py``
    斷言 ``"# verify-clean.sh" in cp.stdout`` 與本觀察相反，屬 pre-existing
    bug，不歸本守護測試處理。前版曾有的 ``test_terminal_stdout_is_empty`` /
    ``test_terminal_stderr_is_empty`` 防呆已移除——理由是這條是**架構事實**
    （block-level redirect 結構自然結果）不是契約，把「架構事實」包成
    「契約斷言」會誤導接手者以為「保護 stdout 為空」是設計意圖。
  - fetch 失敗情境
  - close-out 文件撰寫
  - 假性 diff 排除政策
  上述均屬既有測試或其他任務範疇。

設計紀律：
  - 純 pytest + subprocess 黑盒；不注入 TMPDIR、不 mock。
  - 沿用既有 ``LC_ALL=C`` + ``GIT_TERMINAL_PROMPT=0`` env 慣例。
  - 正/負樣成對 parametrize 形式 (present, absent, why_present, why_absent)，
    對齊 CONTRIBUTING.md:30「守護測試必含 ≥1 個負樣斷言」規範。
  - 用 ``_evidence`` fixture 讀 stdout 宣告的每輪 manifest，
    避免重複「跑 script + manifest + 讀檔」邏輯。
  - 每次 fixture 重新跑一次 script（function scope）以保證測試隔離。

已知限制（PM/架構師定案）：
  - WARN_FILE / OUT_FILE 定位走 stdout 宣告的每輪 manifest；固定 artefact 目錄另保留
    ``manifest.env`` latest 指標（honors ``TMPDIR`` env），
    沙箱可能落 ``/tmp/claude-*``、本地可能落 ``/var/folders/*/T/``、CI 落 ``/tmp``。
    沙箱若動態切換 ``TMPDIR`` 需重跑基準化。
  - 連跑 race 由腳本跑前清舊版根目錄 artefact，並用每輪 manifest 明指本輪
    OUT_FILE / WARN_FILE，杜絕 glob 最新檔與 latest 指標覆寫歧義。

執行：``pytest tests/qa_test_verification_warning_contract.py -v``
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# 與既有 _run() 對齊：固定 locale + 關閉 git 互動 prompt，
# 確保不同 CI 環境下字串比對穩定。
_BASE_ENV = {**os.environ, "LC_ALL": "C", "GIT_TERMINAL_PROMPT": "0"}

# 抓 WARN_FILE 宣告行的 regex。
# 腳本 OUT_FILE 標頭格式：
# `# stderr warning 檔 : <TMPDIR>/verify-clean-artifacts/git-warnings-...-<TS>-<pid>.log`
_DECLARE_RE = re.compile(r"^# stderr warning 檔 : (\S+)\s*$", re.MULTILINE)

# 抓本輪 manifest 宣告行；不讀 latest manifest，避免 xdist 併發覆寫。
_MANIFEST_RE = re.compile(r"^# artefact manifest : (\S+)\s*$", re.MULTILINE)


def _manifest_file(stdout_text: str) -> Path:
    """從 stdout 標頭回傳本輪 ``verify-clean.sh`` manifest 路徑。

    舊版測試用 glob/mtime 抓最新檔，closure-qa 重跑時會被殘留 artefact 誤導；
    現在固定讀本輪 manifest，缺 manifest 宣告就直接紅。
    """
    match = _MANIFEST_RE.search(stdout_text)
    assert match, (
        "stdout 缺 '# artefact manifest : <path>' 宣告行，無法唯一定位本輪證據檔\n"
        f"stdout 前 15 行：\n{chr(10).join(stdout_text.splitlines()[:15])}"
    )
    return Path(match.group(1))


def _read_manifest(path: Path) -> dict[str, str]:
    assert path.is_file(), f"manifest 不存在：{path}"
    data: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        assert sep, f"manifest 行缺 '='：{line!r}"
        data[key] = value
    return data


# --- fixture ---------------------------------------------------------------


@pytest.fixture
def _evidence() -> Iterator[tuple[Path, Path, str]]:
    """跑一次 verify-clean.sh，回傳 (out_file, warn_file, stdout_text) 三元組。

    - out_file / warn_file：manifest 指定的本輪證據檔
    - stdout_text：subprocess 捕獲到的 stdout（黑盒觀察用）

    不再掃 ``git-warnings-*.log`` / ``clean-verify-output-*.txt`` glob，也不讀
    latest manifest；stdout 宣告的每輪 manifest 是唯一索引。
    """
    proc = subprocess.run(
        ["bash", str(REPO_ROOT / "scripts" / "verify-clean.sh")],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        env=_BASE_ENV,
    )

    # 腳本主流程先把 stdout 導到 OUT_FILE，結尾再 cat OUT_FILE；
    # 因此本測試可從 proc.stdout 的標頭取得本輪 manifest 路徑。
    manifest_file = _manifest_file(proc.stdout)
    manifest = _read_manifest(manifest_file)

    required_keys = {
        "artifact_dir",
        "manifest_file",
        "latest_manifest_file",
        "out_file",
        "warn_file",
        "run_id",
        "run_time_utc",
    }
    missing = sorted(required_keys - manifest.keys())
    assert not missing, f"manifest 缺必要欄位：{missing}; manifest={manifest}"

    assert Path(manifest["manifest_file"]) == manifest_file
    artifact_dir = Path(manifest["artifact_dir"])
    assert artifact_dir == manifest_file.parent
    assert Path(manifest["latest_manifest_file"]) == artifact_dir / "manifest.env"

    warn_file = Path(manifest["warn_file"])
    out_file = Path(manifest["out_file"])
    assert warn_file.parent == artifact_dir
    assert out_file.parent == artifact_dir
    assert warn_file.is_file(), f"manifest 指向的 WARN_FILE 不存在：{warn_file}"
    assert out_file.is_file(), f"manifest 指向的 OUT_FILE 不存在：{out_file}"

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


# --- 契約 (c)：分流契約（防呆 / 目前 vacuous truth，但採正/負樣成對）---------


@pytest.mark.parametrize(
    "present_substr,absent_substr,why_present,why_absent",
    [
        pytest.param(
            "Preparing worktree",
            "# verify-clean.sh 結構化輸出",
            "git worktree add 必然 stderr『Preparing worktree (detached HEAD ...)』"
            "——WARN_FILE 收 stderr 的最小存在證據（環境依賴性低, 不依賴 .gitmodules 設定）",
            "OUT_FILE 業務標頭不應誤流到 WARN_FILE（分流契約：stderr 與 stdout 檔分離）",
            id="out-header-routing",
        ),
        pytest.param(
            "Preparing worktree",
            "## Step ",
            "同上: stderr 必落入 WARN_FILE（block-level 2> 結構的自然結果）",
            "OUT_FILE 的 Step 區段標題不應誤流到 WARN_FILE（重構時的 catch 網）",
            id="step-header-routing",
        ),
    ],
)
def test_warn_file_routing_contract(
    _evidence: tuple[Path, Path, str],
    present_substr: str,
    absent_substr: str,
    why_present: str,
    why_absent: str,
) -> None:
    """分流契約：WARN_FILE 必含 stderr 累積（正樣）、絕不含 OUT_FILE 業務標頭（負樣）。

    正/負樣成對 parametrize 形式 (present_substr, absent_substr, why_present, why_absent)，
    對齊 CONTRIBUTING.md:30 守護測試規範「每個 parametrize 配 ≥1 條負樣斷言」+
    補上對應正樣形成對稱，避免「全綠自欺」（無正樣時 absent 通過只能證明
    「absent 不在」，無法證明「WARN_FILE 真的收了 stderr」）。

    **為何用「Preparing worktree」當弱正樣標的**:
      - git worktree add 必然 stderr 此行（除非傳 ``--quiet``，腳本未傳）
      - 環境依賴性低：與 lane 端 ``.gitmodules`` / submodule 設定無關
      - 任何「block-level redirect 2>"$WARN_FILE"」結構下必出現
      - 若腳本改用 ``--quiet`` 讓此行消失，本測試會 fail——是 feature 不是 bug

    **目前 (c) 整體為 vacuous truth 的部分**:
      正樣 (present) 因腳本必然產 stderr 而總是成立;
      負樣 (absent) 因 block-level redirect 結構自然成立而總是成立。
      保留為**重構時的 catch 網**——若日後有人拆成逐命令處理
      （如每個 ``cat $X_ERR >> WARN_FILE`` 漏接），此條會 fail。
    """
    _, warn_file, _ = _evidence
    warn_text = warn_file.read_text(encoding="utf-8", errors="replace")
    # 正樣：WARN_FILE 必含真實 stderr 累積
    assert present_substr in warn_text, (
        f"WARN_FILE 缺『{present_substr}』（{why_present}）\n--- WARN_FILE 內容 ---\n{warn_text}"
    )
    # 負樣：WARN_FILE 絕不含 OUT_FILE 業務標頭
    assert absent_substr not in warn_text, (
        f"WARN_FILE 誤含 OUT_FILE 業務標頭『{absent_substr}』（{why_absent}）\n"
        f"--- WARN_FILE 內容 ---\n{warn_text}"
    )
