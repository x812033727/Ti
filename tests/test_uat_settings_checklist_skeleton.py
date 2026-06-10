"""QA 驗證：任務 #1 設定頁 UAT checklist 文件骨架是否符合驗收標準。

僅驗「骨架」層級（標題／前置條件／測試環境／固定 7 欄表頭／分區塊／
填寫說明＋彙總＋簽核）。各區塊內的具體案例由後續任務填入，不在此驗。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

DOC = Path(__file__).resolve().parents[1] / "docs" / "uat-settings-checklist.md"

EXPECTED_HEADER_COLS = [
    "#",
    "功能區塊",
    "操作步驟",
    "預期結果",
    "實際結果",
    "Pass/Fail",
    "備註",
]


@pytest.fixture(scope="module")
def text() -> str:
    assert DOC.exists(), f"找不到 checklist 文件：{DOC}"
    return DOC.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def lines(text: str) -> list[str]:
    return text.splitlines()


def _split_row(row: str) -> list[str]:
    """把 markdown 表格列拆成各欄（去頭尾 | 與空白）。"""
    cells = row.strip().strip("|").split("|")
    return [c.strip() for c in cells]


def test_文件存在且非空(text: str):
    assert text.strip(), "文件為空"


def test_有第一層標題(lines: list[str]):
    h1 = [ln for ln in lines if re.match(r"^#\s+\S", ln)]
    assert h1, "缺少 H1 標題（# 開頭）"


def test_有前置條件區段(text: str):
    assert "## 前置條件" in text, "缺少『前置條件』區段"


def test_前置條件含啟動指令_登入_開啟設定面板(text: str):
    # 啟動 server 指令
    assert "studio.server" in text, "前置條件未說明如何啟動 server（studio.server）"
    # 進入網址
    assert "localhost:8000" in text, "前置條件未說明進入的網址"
    # 登入
    assert "登入" in text, "前置條件未提到登入"
    # 開啟設定面板
    assert "設定" in text and ("面板" in text or "⚙" in text), "前置條件未說明開啟設定面板"


def test_有測試環境區段且含OS與瀏覽器(text: str):
    assert "## 測試環境" in text, "缺少『測試環境』區段"
    assert re.search(r"作業系統|OS", text), "測試環境未含 OS 欄位"
    assert "瀏覽器" in text, "測試環境未含瀏覽器欄位"


def test_存在固定七欄表頭(lines: list[str]):
    """至少要有一個完全符合規定 7 欄的表頭。"""
    found = []
    for ln in lines:
        if ln.lstrip().startswith("|"):
            cols = _split_row(ln)
            if cols == EXPECTED_HEADER_COLS:
                found.append(ln)
    assert found, (
        "找不到符合規定的固定欄位表頭：" + "｜".join(EXPECTED_HEADER_COLS)
    )


def test_每個案例區塊都用規定七欄表頭(lines: list[str]):
    """① ~ ⑥ 每個案例分區塊下方都應有規定 7 欄表頭。"""
    section_marks = ["①", "②", "③", "④", "⑤", "⑥"]
    # 找出各分區塊標題行索引
    headers_after = {}
    for idx, ln in enumerate(lines):
        if ln.startswith("## ") and any(m in ln for m in section_marks):
            mark = next(m for m in section_marks if m in ln)
            # 在此標題之後、下一個 ## 之前，找 7 欄表頭
            ok = False
            for j in range(idx + 1, len(lines)):
                if lines[j].startswith("## "):
                    break
                if lines[j].lstrip().startswith("|"):
                    if _split_row(lines[j]) == EXPECTED_HEADER_COLS:
                        ok = True
                        break
            headers_after[mark] = ok
    missing = [m for m in section_marks if not headers_after.get(m)]
    assert not missing, f"下列案例區塊缺少規定 7 欄表頭：{missing}"


def test_六大區塊齊全(text: str):
    """依研究結論分區塊：頁面載入→欄位輸入→儲存/取消/重新部署/改密碼→持久性→錯誤/權限→跨瀏覽器/無障礙。"""
    required = [
        "頁面載入",
        "欄位輸入",  # 各欄位輸入與驗證
        "儲存",  # 儲存／取消／重新部署／改密碼
        "持久性",
        "錯誤",  # 錯誤／權限狀態
        "無障礙",  # 跨瀏覽器／無障礙
    ]
    missing = [k for k in required if k not in text]
    assert not missing, f"缺少區塊關鍵字：{missing}"


def test_有填寫說明(text: str):
    assert "## 填寫說明" in text, "缺少『填寫說明』區段（供 QA 接手）"


def test_有彙總區含PassFail統計(text: str):
    assert "## 彙總" in text, "缺少『彙總』區段"
    # 彙總表頭應含 Pass / Fail 統計欄
    assert "Pass" in text and "Fail" in text, "彙總未含 Pass/Fail 統計"


def test_有簽核欄(text: str):
    assert "## 簽核" in text, "缺少『簽核』區段"


def test_前置條件分組對齊settings實際分組(text: str):
    """設定頁實際 group：一般/Claude/OpenAI/GitHub/並行，文件前置條件應對齊。"""
    for grp in ["一般", "Claude", "OpenAI", "GitHub", "並行"]:
        assert grp in text, f"前置條件未提到實際分組：{grp}"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
