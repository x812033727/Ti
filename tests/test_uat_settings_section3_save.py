"""QA 驗證：任務 #4 「③ 儲存／取消／重新部署／改密碼」區塊案例。

驗收標準（任務 #4）：
- 四主題覆蓋：儲存、取消（放棄變更）、重新部署、改密碼。
- 高風險點：連點儲存不重複送出 案例存在。
- 改密碼三欄一致性（長度檢查、兩次一致）案例存在。
- 案例描述須與前端 app.js **實際行為／字串一致**（避免誤導測試員）：
    * 文件宣稱的提示字、確認對話框文字確實存在於 app.js。
    * 文件對「儲存鈕未防連點 / 重新部署鈕有防連點」的對比描述，與程式碼一致。
    * 改密碼前端驗證規則（new<4、new!==confirm）確實存在於程式碼。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / "uat-settings-checklist.md"
APP_JS = ROOT / "web" / "app.js"
HEADER = ["#", "功能區塊", "操作步驟", "預期結果", "實際結果", "Pass/Fail", "備註"]


def _split(row: str) -> list[str]:
    return [c.strip() for c in row.strip().strip("|").split("|")]


def _section(text: str, mark: str, nxt: str) -> str:
    return text.split(f"## {mark}", 1)[1].split(f"## {nxt}", 1)[0]


def _rows(sec: str) -> list[list[str]]:
    out, seen = [], False
    for ln in sec.splitlines():
        if not ln.lstrip().startswith("|"):
            continue
        cols = _split(ln)
        if cols == HEADER:
            seen = True
            continue
        if set("".join(cols)) <= set("- "):
            continue
        if seen and cols[0] and not cols[0].startswith("<!--"):
            out.append(cols)
    return out


@pytest.fixture(scope="module")
def text() -> str:
    return DOC.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def sec(text):
    return _section(text, "③", "④")


@pytest.fixture(scope="module")
def rows(sec):
    return _rows(sec)


@pytest.fixture(scope="module")
def app() -> str:
    return APP_JS.read_text(encoding="utf-8")


# ---------- 格式 ----------


def test_有實際案例(rows):
    assert len(rows) >= 10, f"③ 案例過少：{len(rows)}"


def test_每列七欄關鍵欄非空(rows):
    for r in rows:
        assert len(r) == 7, f"欄數不為 7：{r}"
        assert r[2] and r[3], f"案例 {r[0]} 缺步驟或預期結果"


def test_編號唯一(rows):
    ids = [r[0] for r in rows]
    assert len(ids) == len(set(ids)), f"編號重複：{ids}"


# ---------- 四主題覆蓋 ----------


def test_四主題皆覆蓋(sec):
    for kw in ["儲存", "取消", "重新部署", "改密碼" if "改密碼" in sec else "密碼"]:
        assert kw in sec, f"③ 缺主題：{kw}"
    assert "密碼" in sec, "③ 缺改密碼相關案例"


# ---------- 高風險：連點儲存不重複送出 ----------


def test_有連點儲存案例(sec):
    assert "連點" in sec, "③ 缺『連點儲存不重複送出』高風險案例"
    # 該案例應引導觀察是否送出多筆請求
    assert "Network" in sec or "多筆" in sec or "重複" in sec, "連點案例未引導觀察重複送出"


def test_儲存鈕未防連點_文件與程式碼一致(sec, app):
    """文件 3.2 備註宣稱『儲存鈕請求期間未禁用』，須與 app.js 實際一致。"""
    # 擷取 saveSettings 函式體
    m = re.search(r"async function saveSettings\(\)\s*\{(.*?)\n\}", app, re.S)
    assert m, "找不到 saveSettings 函式"
    body = m.group(1)
    assert "disabled" not in body, (
        "app.js saveSettings 已加入 disabled，但文件仍宣稱未禁用——文件需同步更新"
    )
    # 文件確實有揭露此風險
    assert "未禁用" in sec or "未禁" in sec, "文件未揭露儲存鈕未防連點的風險"


def test_重新部署鈕有防連點_文件與程式碼一致(sec, app):
    m = re.search(r"async function redeployNow\(\)\s*\{(.*?)\n\}", app, re.S)
    assert m, "找不到 redeployNow 函式"
    body = m.group(1)
    assert "btn.disabled = true" in body, "redeployNow 未禁用按鈕，與文件 3.8 不符"
    assert "防連點" in sec or "禁用" in sec, "文件未描述重新部署鈕的防連點行為"


# ---------- 改密碼三欄一致性（前端規則一致）----------


def test_改密碼規則存在於程式碼(app):
    m = re.search(r"async function savePassword\(\)\s*\{(.*?)\n\}", app, re.S)
    assert m, "找不到 savePassword 函式"
    body = m.group(1)
    assert re.search(r"next\.length\s*<\s*4", body), "缺新密碼長度<4 檢查"
    assert re.search(r"next\s*!==\s*confirm", body), "缺兩次新密碼一致檢查"
    assert "至少 4 個字元" in body, "缺長度錯誤提示字"
    assert "兩次輸入的新密碼不一致" in body, "缺不一致錯誤提示字"


def test_改密碼三欄一致性案例齊全(sec):
    assert "至少 4 個字元" in sec, "③ 缺『新密碼長度』案例（3.10）"
    assert "不一致" in sec, "③ 缺『兩次新密碼一致性』案例（3.11）"
    assert "目前密碼" in sec, "③ 缺『目前密碼』相關案例（3.12/3.13）"


# ---------- 文件宣稱字串 vs app.js 一致 ----------


@pytest.mark.parametrize(
    "claim",
    [
        "儲存中…",
        "已儲存，下次討論即生效。",
        "儲存請求失敗",
        "重新部署中…",
        "正在拉取最新 main 並重啟…",
        "已變更，新密碼即時生效。",
    ],
)
def test_文件提到的字串都真實存在於前端(claim, sec, app):
    if claim in sec:
        assert claim in app, f"文件提到字串『{claim}』但 app.js 無此字串（描述失準）"


def test_重新部署確認文字一致(sec, app):
    assert "服務會重啟" in app and "進行中的工作與連線會中斷" in app, (
        "app.js 重新部署 confirm 文字與預期不符"
    )
    if "重新部署" in sec:
        assert "服務會重啟" in sec, "文件 3.5 未對齊實際 confirm 文字"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
