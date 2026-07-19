"""QA 驗證：任務 #2 「① 頁面載入與現值顯示」區塊案例。

驗收標準（任務 #2）：
- ① 區塊有實際案例（非佔位），每列 7 欄、單一驗證點。
- 覆蓋三大主題：開啟設定面板、分組顯示、秘密欄位只顯示「是否已設定」不顯示明文。
- 案例描述須與前端／後端**實際行為一致**（避免誤導測試員）：
    * 文件宣稱的字串（載入中…、已設定（留空＝不變更）、設定/關閉圖示鈕）確實存在於原始碼。
    * 分組順序與 settings.FIELDS 實際 group 順序一致。
    * 後端 settings.read() 對秘密欄確實不回明文（value=""、set=True）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / "uat-settings-checklist.md"
# 設定面板前端已拆為 ES module：字串一致性斷言對準 settings 面板模組
APP_JS = ROOT / "web" / "js" / "panels" / "settings.js"
INDEX_HTML = ROOT / "web" / "index.html"

EXPECTED_HEADER_COLS = ["#", "功能區塊", "操作步驟", "預期結果", "實際結果", "Pass/Fail", "備註"]


def _split_row(row: str) -> list[str]:
    return [c.strip() for c in row.strip().strip("|").split("|")]


def _section_rows(text: str, mark: str) -> list[list[str]]:
    """取出某 ## 區塊（以 mark 如『①』標示）下、表頭之後的資料列。"""
    lines = text.splitlines()
    rows: list[list[str]] = []
    in_sec = False
    seen_header = False
    for ln in lines:
        if ln.startswith("## "):
            in_sec = mark in ln
            seen_header = False
            continue
        if not in_sec or not ln.lstrip().startswith("|"):
            continue
        cols = _split_row(ln)
        if cols == EXPECTED_HEADER_COLS:
            seen_header = True
            continue
        if set("".join(cols)) <= set("- "):  # 分隔線 ---
            continue
        if seen_header:
            rows.append(cols)
    return rows


@pytest.fixture(scope="module")
def text() -> str:
    return DOC.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def rows(text: str) -> list[list[str]]:
    return _section_rows(text, "①")


# ---- 格式類 ----


def test_區塊有實際案例非佔位(rows):
    real = [r for r in rows if r and r[0] and not r[0].startswith("<!--")]
    assert len(real) >= 5, f"① 區塊案例過少或仍為佔位，實得 {len(real)} 列"


def test_每列七欄且關鍵欄非空(rows):
    real = [r for r in rows if r and r[0] and not r[0].startswith("<!--")]
    for r in real:
        assert len(r) == 7, f"列欄數不為 7：{r}"
        no, _blk, step, expect = r[0], r[1], r[2], r[3]
        assert step, f"案例 {no} 缺操作步驟"
        assert expect, f"案例 {no} 缺預期結果"


def test_案例編號唯一(rows):
    real = [r[0] for r in rows if r and r[0] and not r[0].startswith("<!--")]
    assert len(real) == len(set(real)), f"案例編號重複：{real}"


# ---- 三大主題覆蓋 ----


def test_涵蓋開啟設定面板(text):
    sec = text.split("## ①", 1)[1].split("## ②", 1)[0]
    assert "設定" in sec and ("面板" in sec or "彈窗" in sec), "① 未涵蓋『開啟設定面板』"


def test_涵蓋分組顯示(text):
    sec = text.split("## ①", 1)[1].split("## ②", 1)[0]
    assert "分組" in sec, "① 未涵蓋『分組顯示』"


def test_涵蓋秘密欄只顯示是否已設定(text):
    sec = text.split("## ①", 1)[1].split("## ②", 1)[0]
    assert "明文" in sec, "① 未明確要求秘密欄不顯示明文"
    assert "已設定" in sec, "① 未涵蓋秘密欄顯示『已設定』狀態"


# ---- 文件宣稱 vs 原始碼事實 一致性 ----


def test_載入中字樣存在於前端(text):
    sec = text.split("## ①", 1)[1].split("## ②", 1)[0]
    if "載入中" in sec:
        assert "載入中" in APP_JS.read_text(encoding="utf-8"), "文件提到『載入中…』但前端無此字串"


def test_秘密提示字與前端一致(text):
    sec = text.split("## ①", 1)[1].split("## ②", 1)[0]
    app = APP_JS.read_text(encoding="utf-8")
    if "留空＝不變更" in sec:
        assert "留空＝不變更" in app, "文件秘密欄提示字與前端 app.js 不一致"


def test_設定按鈕與關閉鈕存在於index(text):
    # 工具列已換 SVG 線性圖示：設定/關閉為純圖示鈕，以 id + aria-label 驗證存在與可及名稱
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert (
        'id="settingsBtn"' in html and 'aria-label="設定"' in html
    ), "index.html 無設定按鈕（id=settingsBtn＋aria-label=設定），案例 1.1 描述失準"
    assert 'id="settingsClose"' in html and 'aria-label="關閉"' in html, "index.html 無關閉鈕"
    assert 'id="settingsSave"' in html, "index.html 無『儲存』按鈕"


def test_分組順序與settings一致(text):
    from studio import settings

    seen: list[str] = []
    for f in settings.FIELDS:
        if f.group and f.group not in seen:
            seen.append(f.group)
    # 文件 ① 區塊宣稱的順序
    sec = text.split("## ①", 1)[1].split("## ②", 1)[0]
    # 取文件中各分組名第一次出現位置，依此排序
    positions = []
    for g in seen:
        idx = sec.find(g)
        assert idx != -1, f"① 區塊未提到分組：{g}"
        positions.append((idx, g))
    doc_order = [g for _, g in sorted(positions)]
    assert doc_order == seen, f"分組順序不符：文件={doc_order} 實際={seen}"


# ---- 後端真實行為：秘密欄不回明文 ----


def test_後端秘密欄不回明文(monkeypatch):
    from studio import settings

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-SHOULD-NOT-LEAK")
    data = settings.read()
    fld = next(x for x in data["fields"] if x["env"] == "ANTHROPIC_API_KEY")
    assert fld["secret"] is True
    assert fld["set"] is True, "已設定的秘密欄 set 應為 True"
    assert fld["value"] == "", f"秘密欄洩漏明文：{fld['value']!r}"
    assert "SHOULD-NOT-LEAK" not in str(data), "read() 回傳內容含明文金鑰"


def test_非秘密欄回傳現值(monkeypatch):
    from studio import settings

    monkeypatch.setenv("TI_MODEL_LEAD", "claude-opus-4-8")
    data = settings.read()
    fld = next(x for x in data["fields"] if x["env"] == "TI_MODEL_LEAD")
    assert fld["secret"] is False
    assert fld["value"] == "claude-opus-4-8", "非秘密欄應回傳現值供 UI 顯示"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
