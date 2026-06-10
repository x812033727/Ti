"""QA 驗證：任務 #5 「④ 持久性 / ⑤ 錯誤·權限 / ⑥ 跨瀏覽器·無障礙」案例。

驗收標準（任務 #5）：
- 儲存後重整值還在、秘密欄留空不清空、未存變更離開有提醒、跨瀏覽器與鍵盤無障礙。
- 三高風險點必含：連點儲存不重複送出、未存變更離開有提醒、儲存後重整值還在。
- 案例描述與實際行為一致：
    * 4.6 宣稱「前端未實作 beforeunload 離開提醒」須與程式碼一致（誠實揭露）。
    * 6.8 宣稱「label 包覆 input」須與 renderSettings 一致。
    * ⑤ 宣稱的後端權限／錯誤行為（401/403/400）須與 routes.py 一致。
- 真實行為佐證：以實際檔案 round-trip 證明持久化與「留空不清空」，並驗秘密檔 0600。
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / "uat-settings-checklist.md"
APP_JS = ROOT / "web" / "app.js"
INDEX_HTML = ROOT / "web" / "index.html"
ROUTES = ROOT / "studio" / "routes.py"
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
def sec4(text):
    return _section(text, "④", "⑤")


@pytest.fixture(scope="module")
def sec5(text):
    return _section(text, "⑤", "⑥")


@pytest.fixture(scope="module")
def sec6(text):
    return _section(text, "⑥", "填寫說明") if "## 填寫說明" in text else _section(text, "⑥", "彙總")


# ---------- 格式 ----------


def test_各區塊有實際案例且七欄(sec4, sec5, sec6):
    for name, sec in [("④", sec4), ("⑤", sec5), ("⑥", sec6)]:
        rows = _rows(sec)
        assert len(rows) >= 4, f"{name} 案例過少：{len(rows)}"
        for r in rows:
            assert len(r) == 7, f"{name} 列欄數不為 7：{r}"
            assert r[2] and r[3], f"{name} 案例 {r[0]} 缺步驟或預期結果"


def test_編號全域唯一(sec4, sec5, sec6):
    ids = [r[0] for sec in (sec4, sec5, sec6) for r in _rows(sec)]
    assert len(ids) == len(set(ids)), f"編號重複：{ids}"


# ---------- ④ 持久性四重點 ----------


def test_儲存後重整值還在(sec4):
    assert "重整" in sec4 or "重新整理" in sec4 or "F5" in sec4, "④ 缺『重整』案例"
    assert "還在" in sec4 or "仍顯示" in sec4 or "仍在" in sec4, "④ 未描述值保留"


def test_秘密欄留空不清空(sec4):
    assert "留空" in sec4 and ("不變更" in sec4 or "未被清空" in sec4 or "不清空" in sec4), (
        "④ 缺『秘密欄留空不清空』案例"
    )


def test_未存變更離開提醒(sec4):
    assert "離開" in sec4 or "未儲存" in sec4 or "未存" in sec4, "④ 缺『未存變更離開提醒』案例"


# ---------- 三高風險點（全域必含）----------


def test_三高風險點全部出現(text):
    assert "連點" in text, "缺高風險點：連點儲存不重複送出"
    assert "離開" in text and ("未儲存" in text or "未存" in text), "缺高風險點：未存變更離開提醒"
    assert ("重整" in text or "重新整理" in text) and ("還在" in text or "仍" in text), (
        "缺高風險點：儲存後重整值還在"
    )


# ---------- ⑤ 錯誤／權限覆蓋 ----------


def test_錯誤權限案例覆蓋(sec5):
    assert "401" in sec5, "⑤ 缺未授權 401 案例"
    assert "403" in sec5, "⑤ 缺來源限制 403 案例"
    assert "400" in sec5, "⑤ 缺格式錯誤 400 案例"
    assert "無法載入" in sec5, "⑤ 缺載入失敗回饋案例"


# ---------- ⑥ 跨瀏覽器／無障礙覆蓋 ----------


def test_跨瀏覽器至少兩種引擎(sec6):
    browsers = [b for b in ["Chrome", "Firefox", "Safari", "Edge"] if b in sec6]
    assert len(browsers) >= 2, f"⑥ 跨瀏覽器引擎不足：{browsers}"


def test_鍵盤與報讀無障礙覆蓋(sec6):
    assert "鍵盤" in sec6 and "Tab" in sec6, "⑥ 缺鍵盤走訪案例"
    assert "報讀" in sec6 or "螢幕" in sec6, "⑥ 缺螢幕報讀案例"
    assert "焦點" in sec6, "⑥ 缺焦點可見性描述"


# ---------- 文件宣稱 vs 程式碼事實 ----------


def test_未實作離開提醒_與程式碼一致(sec4):
    """4.6 宣稱前端無 beforeunload；須與實際程式碼一致（誠實揭露待開發項）。"""
    app = APP_JS.read_text(encoding="utf-8")
    html = INDEX_HTML.read_text(encoding="utf-8")
    has_beforeunload = "beforeunload" in app or "beforeunload" in html
    claims_not_impl = "beforeunload" in sec4 or "未實作" in sec4
    if not has_beforeunload:
        assert claims_not_impl, "前端確實無 beforeunload，但文件未誠實標註『未實作離開提醒』"
    else:
        # 若哪天前端加了 beforeunload，文件描述需同步更新
        assert "未實作" not in sec4, "程式碼已實作 beforeunload，文件仍稱未實作——需更新"


def test_label包覆input_與renderSettings一致(sec6):
    app = APP_JS.read_text(encoding="utf-8")
    assert 'createElement("label")' in app, "renderSettings 未以 label 包覆欄位，6.8 描述失準"


def test_routes後端權限與錯誤防護屬實(sec5):
    src = ROUTES.read_text(encoding="utf-8")
    # GET /api/settings 受 require_auth
    assert 'get("/api/settings", dependencies=[Depends(auth.require_auth)])' in src, (
        "GET /api/settings 未掛 require_auth，5.2 失準"
    )
    # POST /api/settings 受 loopback + auth（WRITE_DEPS）
    assert 'post("/api/settings", dependencies=WRITE_DEPS)' in src, (
        "POST /api/settings 未掛 WRITE_DEPS，5.3 失準"
    )
    assert "require_loopback" in src and "require_auth" in src, "WRITE_DEPS 未含雙重保護"
    # 非 dict body → 400 格式錯誤
    assert "isinstance(body, dict)" in src and '"格式錯誤"' in src and "status_code=400" in src, (
        "POST 未對非物件 body 回 400 格式錯誤，5.4 失準"
    )


# ---------- 真實行為佐證：持久化 round-trip + 0600 ----------


def test_持久化roundtrip_改其他欄原值保留(tmp_path):
    """模擬『儲存後重整值還在』『改其他欄不影響既有值』。"""
    from dotenv import get_key

    from studio.secretfile import write_secret_file

    env = str(tmp_path / ".env")
    write_secret_file(env, "TI_MODEL_LEAD", "claude-opus-4-8")
    write_secret_file(env, "GITHUB_TOKEN", "ghp_secret_xyz")
    # 再改另一欄，模擬「只改一欄、其他欄留著」
    write_secret_file(env, "TI_PARALLEL_LANES", "6")

    assert get_key(env, "TI_MODEL_LEAD") == "claude-opus-4-8", "重整後文字欄值應保留"
    assert get_key(env, "GITHUB_TOKEN") == "ghp_secret_xyz", (
        "改其他欄後既有金鑰應保留（留空不清空的底層保證）"
    )
    assert get_key(env, "TI_PARALLEL_LANES") == "6", "新值應寫入"


def test_秘密檔權限為0600(tmp_path):
    import stat

    from studio.secretfile import write_secret_file

    env = str(tmp_path / ".env")
    write_secret_file(env, "ANTHROPIC_API_KEY", "sk-ant-secret")
    mode = stat.S_IMODE((tmp_path / ".env").stat().st_mode)
    assert mode == 0o600, f"秘密 .env 權限應為 0600，實際 {oct(mode)}（避免金鑰被他人讀取）"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
