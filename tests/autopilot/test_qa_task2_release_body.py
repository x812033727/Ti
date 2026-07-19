"""QA 任務 #2：`scripts/publish_release.py` release body 取值介面驗收。

驗收焦點（對應整體 AC#2、AC#5 與任務描述）：
  - body 來源 = `render_tag_notes(讀 CHANGELOG.md, pyproject_version())`，版本走 SSOT。
  - 寫入 `$GITHUB_OUTPUT`，比照 release-smoke.yml 既有 BODY 慣例（`name<<隨機分隔符` 區塊）。
  - 腳本內**零** Breaking heading 字面值（grep 0 命中），且附反向 mutation 證明 grep 有判別力。
  - 失敗路徑（缺 Breaking 區塊）：例外向上拋 → 非零退出，且舊 body.md 不殘留（冪等性）。

破壞性思考：本檔重點打「快樂路徑以外」——缺區塊、舊檔殘留、分隔符可預測性、
GITHUB_OUTPUT round-trip 可被下游正確解析、append 不覆蓋。
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

# 以 SSOT 常數構造 fixture，避免在測試裡再寫一份 heading 字面值。
from studio.release_note import BREAKING_HEADING, MissingBreakingBlock, pyproject_version

_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _ROOT / "scripts" / "publish_release.py"


def _load_publish_release():
    """直接 import scripts/publish_release.py（非套件路徑），回傳 module 物件。"""
    spec = importlib.util.spec_from_file_location("publish_release", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["publish_release"] = mod
    spec.loader.exec_module(mod)
    return mod


pr = _load_publish_release()


# fixture CHANGELOG：含非空頂層 Breaking 區塊（用 SSOT 常數拼，不硬寫字面值）。
_CHANGELOG_WITH_BLOCK = (
    "# Changelog\n\n"
    f"{BREAKING_HEADING}\n\n"
    "- 行為變動：X 預設改 strict。\n"
    "- 原因：安全強化。\n\n"
    "## 0.9.9 - 2026-01-01\n\n- 其他變更。\n"
)

# fixture CHANGELOG：完全沒有 Breaking 區塊（失敗路徑用）。
_CHANGELOG_NO_BLOCK = "# Changelog\n\n## 0.9.9 - 2026-01-01\n\n- 只有一般變更。\n"


# ---------------------------------------------------------------------------
# render_release_body：正常路徑 + SSOT
# ---------------------------------------------------------------------------


def test_render_reads_real_changelog_and_pyproject_version():
    """不帶參數時：讀真實 CHANGELOG.md + pyproject_version()，body 非空且含 Breaking 區塊。"""
    body = pr.render_release_body()
    assert body.strip(), "body 不該為空"
    assert (
        BREAKING_HEADING in body
    ), "body 必須含逐字 Breaking heading（來自 render_tag_notes 注入）"
    # 版本來自 SSOT，必須出現在 body（render_tag_notes 的 heading / footer 都帶 version）。
    assert pyproject_version() in body, "body 必須含 pyproject SSOT 版本字串"


def test_render_with_explicit_args(tmp_path):
    """帶顯式 changelog_path + version：讀指定檔，version 逐字進 body。"""
    cl = tmp_path / "CHANGELOG.md"
    cl.write_text(_CHANGELOG_WITH_BLOCK, encoding="utf-8")
    body = pr.render_release_body(changelog_path=cl, version="3.1.4")
    assert "3.1.4" in body
    assert BREAKING_HEADING in body
    assert "X 預設改 strict" in body, "區塊內文必須逐字保留（零截斷）"


def test_render_version_omitted_uses_ssot(tmp_path, monkeypatch):
    """version 省略時走 pyproject_version()——非硬寫；改 SSOT 即反映於 body。"""
    cl = tmp_path / "CHANGELOG.md"
    cl.write_text(_CHANGELOG_WITH_BLOCK, encoding="utf-8")
    monkeypatch.setattr(pr, "pyproject_version", lambda: "7.7.7")
    body = pr.render_release_body(changelog_path=cl)
    assert "7.7.7" in body, "version 省略時應取 pyproject_version() 回傳值，而非寫死"


# ---------------------------------------------------------------------------
# render_release_body：失敗路徑（缺 Breaking 區塊）
# ---------------------------------------------------------------------------


def test_render_missing_block_raises(tmp_path):
    """缺 Breaking 區塊：render_tag_notes 拋 MissingBreakingBlock（不靜默產半截 body）。"""
    cl = tmp_path / "CHANGELOG.md"
    cl.write_text(_CHANGELOG_NO_BLOCK, encoding="utf-8")
    with pytest.raises(MissingBreakingBlock):
        pr.render_release_body(changelog_path=cl, version="1.0.0")


def test_render_missing_changelog_file_raises(tmp_path):
    """CHANGELOG 檔不存在：read_text 拋 FileNotFoundError，不吞錯。"""
    with pytest.raises(FileNotFoundError):
        pr.render_release_body(changelog_path=tmp_path / "nope.md", version="1.0.0")


# ---------------------------------------------------------------------------
# write_github_output：比照既有 BODY 慣例
# ---------------------------------------------------------------------------


def test_github_output_format_matches_body_convention(tmp_path):
    """格式須為 `name<<DELIM\\n{value}\\n{DELIM}\\n`，且下游能 round-trip 還原原值。"""
    out = tmp_path / "gh_out"
    value = "line1\nline2 含 - 開頭與 ## 偽 heading\nline3"
    pr.write_github_output("body", value, out)
    content = out.read_text(encoding="utf-8")

    m = re.match(r"body<<(?P<d>\S+)\n(?P<v>.*)\n(?P=d)\n\Z", content, re.DOTALL)
    assert m, f"GITHUB_OUTPUT 格式不符 heredoc 慣例：{content!r}"
    assert m.group("v") == value, "round-trip 還原值必須等於原 body（零截斷／竄改）"
    assert m.group("d").startswith("BODY_EOF_"), "分隔符前綴須比照 smoke 的 BODY_EOF_"


def test_github_output_delimiter_is_random(tmp_path):
    """分隔符須隨機（避免 body 含固定分隔字串造成截斷／注入）：兩次呼叫分隔符不同。"""
    a, b = tmp_path / "a", tmp_path / "b"
    pr.write_github_output("body", "x", a)
    pr.write_github_output("body", "x", b)
    da = re.match(r"body<<(\S+)\n", a.read_text())
    db = re.match(r"body<<(\S+)\n", b.read_text())
    assert da and db
    assert da.group(1) != db.group(1), "分隔符應隨機，不可固定"


def test_github_output_appends_not_overwrites(tmp_path):
    """寫入須 append（GITHUB_OUTPUT 為累積檔），不可覆蓋既有內容。"""
    out = tmp_path / "gh_out"
    out.write_text("preexisting=keepme\n", encoding="utf-8")
    pr.write_github_output("body", "v", out)
    content = out.read_text(encoding="utf-8")
    assert "preexisting=keepme" in content, "不可覆蓋既有 GITHUB_OUTPUT 內容"
    assert "body<<" in content


# ---------------------------------------------------------------------------
# main()：body.md 落檔 + GITHUB_OUTPUT + 冪等性
# ---------------------------------------------------------------------------


def test_main_writes_bodymd_and_github_output(tmp_path, monkeypatch):
    """main()：寫 body.md（內容==render 結果）並寫 GITHUB_OUTPUT 的 body 鍵。"""
    monkeypatch.setattr(pr, "_ROOT", tmp_path)
    (tmp_path / "CHANGELOG.md").write_text(_CHANGELOG_WITH_BLOCK, encoding="utf-8")
    gh_out = tmp_path / "gh_out"
    monkeypatch.setenv("GITHUB_OUTPUT", str(gh_out))

    rc = pr.main()
    assert rc == 0
    body_md = tmp_path / "body.md"
    assert body_md.exists(), "body.md 必須產出供 gh release create -F 讀取"
    body = body_md.read_text(encoding="utf-8")
    assert BREAKING_HEADING in body

    # GITHUB_OUTPUT 的 body 值 round-trip 必須等於 body.md 內容（兩出口同源、零拼裝）。
    m = re.match(
        r"body<<(?P<d>\S+)\n(?P<v>.*)\n(?P=d)\n\Z", gh_out.read_text(encoding="utf-8"), re.DOTALL
    )
    assert m and m.group("v") == body, "GITHUB_OUTPUT body 必須與 body.md 同源同值"


def test_main_without_github_output_still_writes_bodymd(tmp_path, monkeypatch):
    """未設 GITHUB_OUTPUT（如本機跑）：仍寫 body.md 並回 0，不炸。"""
    monkeypatch.setattr(pr, "_ROOT", tmp_path)
    (tmp_path / "CHANGELOG.md").write_text(_CHANGELOG_WITH_BLOCK, encoding="utf-8")
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    rc = pr.main()
    assert rc == 0
    assert (tmp_path / "body.md").exists()


def test_main_failure_removes_stale_bodymd(tmp_path, monkeypatch):
    """冪等性：render 失敗時，舊 body.md 必須先被清除，不可殘留供下游 step 誤讀。"""
    monkeypatch.setattr(pr, "_ROOT", tmp_path)
    (tmp_path / "CHANGELOG.md").write_text(_CHANGELOG_NO_BLOCK, encoding="utf-8")
    stale = tmp_path / "body.md"
    stale.write_text("舊版殘留內容——絕不能被下游讀到", encoding="utf-8")
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)

    with pytest.raises(MissingBreakingBlock):
        pr.main()
    assert not stale.exists(), "render 失敗後舊 body.md 必須已被刪除（unlink 在 render 前）"


def test_main_failure_does_not_write_github_output(tmp_path, monkeypatch):
    """失敗路徑不可寫出 body 到 GITHUB_OUTPUT（避免下游讀到空/舊值假綠）。"""
    monkeypatch.setattr(pr, "_ROOT", tmp_path)
    (tmp_path / "CHANGELOG.md").write_text(_CHANGELOG_NO_BLOCK, encoding="utf-8")
    gh_out = tmp_path / "gh_out"
    monkeypatch.setenv("GITHUB_OUTPUT", str(gh_out))
    with pytest.raises(MissingBreakingBlock):
        pr.main()
    assert (
        not gh_out.exists() or "body<<" not in gh_out.read_text()
    ), "render 失敗時不應寫出 body 至 GITHUB_OUTPUT"


# ---------------------------------------------------------------------------
# 零 heading 字面值 + 反向 mutation（防孤立假綠）
# ---------------------------------------------------------------------------


def test_script_has_zero_breaking_heading_literal():
    """腳本原始碼 grep 不出 Breaking heading 字面值（0 命中）；heading 一律由 SSOT 注入。"""
    src = _SCRIPT.read_text(encoding="utf-8")
    assert (
        BREAKING_HEADING not in src
    ), "scripts/publish_release.py 不得硬寫 Breaking heading 字面值"
    # 連 emoji 也不該單獨出現（避免拆字繞過）。
    assert "⚠️ Breaking Changes" not in src


def test_grep_mutation_is_meaningful():
    """反向證明上面的 grep 有判別力：同字串確實存在於 SSOT 模組（否則 grep 永遠 0 命中＝假綠）。"""
    ssot_src = (_ROOT / "studio" / "release_note.py").read_text(encoding="utf-8")
    assert (
        BREAKING_HEADING in ssot_src
    ), "SSOT 模組應含 heading 字面值——若這裡都找不到，代表 grep 失效，零命中無意義"
