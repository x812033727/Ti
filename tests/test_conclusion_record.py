"""QA 驗證：conclusion.record 落盤層（任務 #4，驗收 #4/#5/#6）。

涵蓋：① 四鍵結論 → CONCLUSION.md 落 workspace 根、四段固定標題齊全；② 每條結論
原樣保留 (round, speaker) 錨點；③ 覆寫式單檔（每場快照，非 append 累積）；④ fallback
空結論仍產出四段骨架（驗收 #6）；⑤ cwd None 不落盤回 None；⑥ atomic 寫入無殘留 tmp。
"""

from pathlib import Path

from studio import conclusion

_FOUR_HEADERS = ["## 共識", "## 分歧", "## 未決事項", "## 後續行動"]


def _full():
    return {
        "consensus": ["engineer 同意 senior 採混合範式 (R2 engineer)"],
        "disagreements": ["qa 反對 engineer 覆蓋率 (R3 qa)"],
        "open_questions": ["rate limit 門檻未定 (R3 security)"],
        "actions": ["補 e2e 測試自證對應"],
    }


def test_record_writes_four_sections(tmp_path: Path):
    path = conclusion.record(tmp_path, _full(), session_id="s1")
    assert path == tmp_path / "CONCLUSION.md"
    assert path.is_file()
    text = path.read_text(encoding="utf-8")
    # 四段標題齊全且依固定順序（驗收 #4）。
    idxs = [text.index(h) for h in _FOUR_HEADERS]
    assert idxs == sorted(idxs), "四段標題須依固定順序出現"


def test_record_preserves_round_speaker_anchor(tmp_path: Path):
    path = conclusion.record(tmp_path, _full(), session_id="s1")
    text = path.read_text(encoding="utf-8")
    # 錨點原樣保留、可回指 transcript（驗收 #5）。
    assert "(R2 engineer)" in text
    assert "- engineer 同意 senior 採混合範式 (R2 engineer)" in text


def test_record_overwrites_single_file(tmp_path: Path):
    conclusion.record(tmp_path, _full(), session_id="s1")
    # 第二場結論覆寫，舊內容不殘留（每場快照，非 append）。
    conclusion.record(
        tmp_path,
        {"consensus": ["全新共識"], "disagreements": [], "open_questions": [], "actions": []},
        session_id="s2",
    )
    text = (tmp_path / "CONCLUSION.md").read_text(encoding="utf-8")
    assert "全新共識" in text
    assert "engineer 同意 senior" not in text
    # 覆寫後仍只有一份檔、無多餘累積。
    assert text.count("# 討論結論") == 1


def test_record_empty_still_produces_four_sections(tmp_path: Path):
    """fallback 路徑：空結論仍產出四段骨架（標「（無）」），確保 CONCLUSION.md 必有（驗收 #6）。"""
    path = conclusion.record(
        tmp_path,
        {"consensus": [], "disagreements": [], "open_questions": [], "actions": []},
        session_id="s1",
    )
    text = path.read_text(encoding="utf-8")
    for h in _FOUR_HEADERS:
        assert h in text
    assert text.count("（無）") == 4


def test_record_fallback_action_note(tmp_path: Path):
    """蒸餾失靈 fallback dict（行動段標明、不冒充）落盤後可讀到該標記。"""
    fb = conclusion._fallback_from_summary(
        {"consensus": ["a"], "disagreements": [], "open_questions": []}
    )
    path = conclusion.record(tmp_path, fb, session_id="s1")
    text = path.read_text(encoding="utf-8")
    assert conclusion._FALLBACK_ACTION_NOTE in text


def test_record_cwd_none_returns_none():
    assert conclusion.record(None, _full(), session_id="s1") is None


def test_record_no_tmp_residue(tmp_path: Path):
    """atomic tmp-replace 寫入後不留 .md.tmp 殘檔。"""
    conclusion.record(tmp_path, _full(), session_id="s1")
    assert not (tmp_path / "CONCLUSION.md.tmp").exists()
    assert list(tmp_path.glob("*.tmp")) == []


def test_render_markdown_pure_filters_blank_items():
    """render 純函式：剝空白、跳過空字串條目。"""
    md = conclusion.render_markdown(
        {"consensus": ["  x  ", "", "   "], "disagreements": [], "open_questions": [], "actions": []}
    )
    assert "- x" in md
    # 空字串不產生空 bullet；該段只有一條真實內容。
    assert md.count("- ") >= 1
    lines = [l for l in md.splitlines() if l.startswith("- ")]
    assert "- " not in lines  # 無空 bullet
