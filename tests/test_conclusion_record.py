"""QA 驗證：conclusion.record 落盤層（任務 #4，驗收 #4/#5/#6）。

涵蓋：① 四鍵結論 → CONCLUSION.md 落 workspace 根、四段固定標題齊全；② 每條結論
原樣保留 (round, speaker) 錨點；③ 覆寫式單檔（每場快照，非 append 累積）；④ fallback
空結論仍產出四段骨架（驗收 #6）；⑤ cwd None 不落盤回 None；⑥ atomic 寫入無殘留 tmp。
"""

import asyncio
import json
from pathlib import Path

from studio import conclusion
from studio.discussion import DiscussionEngine, Mention, Utterance

_FOUR_HEADERS = ["## 共識", "## 分歧", "## 未決事項", "## 後續行動"]


class _StubSenior:
    """回傳預設文字的假 senior（對齊 conclusion 既有測試慣例）。"""

    def __init__(self, output: str = ""):
        self.output = output

    async def speak(self, prompt, broadcast):
        return self.output


async def _noop(ev):
    pass


def _real_summary(transcript):
    """用真實規則層 _build_summary 推導 summary（consensus 字串無 round 錨點）。"""
    eng = DiscussionEngine([("engineer", _StubSenior())], max_rounds=1)
    return eng._build_summary(transcript)


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


def test_record_does_not_strip_anchor(tmp_path: Path):
    """render/record 只格式化、不刪錨點（注意：此測試的錨點是輸入自帶，只證 record 不丟）。

    錨點「怎麼來」由 test_anchor_provenance_through_pipeline 證明，非此測試。
    """
    path = conclusion.record(tmp_path, _full(), session_id="s1")
    text = path.read_text(encoding="utf-8")
    assert "(R2 engineer)" in text
    assert "- engineer 同意 senior 採混合範式 (R2 engineer)" in text


def test_anchor_provenance_through_pipeline(tmp_path: Path):
    """錨點來源真實性（修補 critic 退回點）：走 summarize→record 完整管線，錨點的
    round/speaker 必須由 transcript 的 Utterance.round 推導，而非任何輸入字串自帶。

    規則層 summary 的 consensus/disagreements 字串本身**不含 round**（如「engineer 同意
    senior」）；落盤後出現的 (R1 engineer)/(R2 qa) 只能來自 transcript——這證明 fallback
    路徑（senior 空輸出）也能產出可回指 transcript 的 CONCLUSION.md（驗收 #5＋#6）。
    """
    transcript = [
        Utterance(1, "engineer", "支持 senior", [Mention("engineer", "senior", "同意")]),
        Utterance(2, "qa", "反對 engineer", [Mention("qa", "engineer", "反對")]),
    ]
    summary = _real_summary(transcript)
    # 前置確認：規則層字串確實不帶 round 錨點（否則此測試證不到 provenance）。
    assert summary["consensus"] == ["engineer 同意 senior"]
    assert "R1" not in summary["consensus"][0]

    # senior 空輸出 → 走 fallback，仍須帶錨點。
    conclusion_dict = asyncio.run(conclusion.summarize(_StubSenior(""), summary, transcript, _noop))
    path = conclusion.record(tmp_path, conclusion_dict, session_id="s1")
    text = path.read_text(encoding="utf-8")
    # 錨點 round=1/2 來自 transcript Utterance.round；speaker 來自 mention.speaker。
    assert "engineer 同意 senior (R1 engineer)" in text
    assert "qa 反對 engineer (R2 qa)" in text
    # 至少一條回指 transcript（驗收 #5 抽查）。
    assert "(R1 engineer)" in text


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
        {"consensus": ["a"], "disagreements": [], "open_questions": []}, []
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


# ── 任務 #3：機讀 conclusion.json sidecar ────────────────────────────────────


def test_record_writes_json_sidecar(tmp_path: Path):
    """record 除 CONCLUSION.md 外另落合法 JSON sidecar，含 version/session_id/rounds＋四鍵（驗收 #3）。"""
    conclusion.record(tmp_path, _full(), session_id="s42", rounds=3)
    jpath = tmp_path / "conclusion.json"
    assert jpath.is_file()
    data = json.loads(jpath.read_text(encoding="utf-8"))  # 合法 JSON
    assert data["version"] == 1
    assert data["session_id"] == "s42"
    assert data["rounds"] == 3
    # 四鍵齊全且內容與輸入一致（機讀入口穩定）。
    for key in ("consensus", "disagreements", "open_questions", "actions"):
        assert data[key] == _full()[key]


def test_record_sidecar_cwd_none_no_file():
    """cwd None → md 與 sidecar 皆不落、回 None（驗收 #3）。"""
    assert conclusion.record(None, _full(), session_id="s1", rounds=2) is None


def test_record_sidecar_overwrites(tmp_path: Path):
    """sidecar 與 md 同為覆寫式單檔，每場快照不累積。"""
    conclusion.record(tmp_path, _full(), session_id="s1", rounds=1)
    conclusion.record(
        tmp_path,
        {"consensus": ["新共識"], "disagreements": [], "open_questions": [], "actions": []},
        session_id="s2",
        rounds=5,
    )
    data = json.loads((tmp_path / "conclusion.json").read_text(encoding="utf-8"))
    assert data["session_id"] == "s2"
    assert data["rounds"] == 5
    assert data["consensus"] == ["新共識"]


def test_record_sidecar_no_tmp_residue(tmp_path: Path):
    """atomic tmp-replace：sidecar 寫後不留 .json.tmp 殘檔（CLAUDE.md 鐵則）。"""
    conclusion.record(tmp_path, _full(), session_id="s1", rounds=1)
    assert not (tmp_path / "conclusion.json.tmp").exists()
    assert list(tmp_path.glob("*.tmp")) == []


def test_record_sidecar_failure_degrades_to_md_only(tmp_path: Path, monkeypatch):
    """sidecar 寫入失敗 → 降級只保留 CONCLUSION.md、不拋例外、回傳 md path 不變（設計決策）。"""

    real_write = conclusion.Path.write_text

    def _selective(self, *a, **k):
        # 只讓 sidecar 的 .json.tmp 寫入失敗，md 正常落盤。
        if self.name.endswith(".json.tmp"):
            raise OSError("disk full")
        return real_write(self, *a, **k)

    monkeypatch.setattr(conclusion.Path, "write_text", _selective, raising=True)
    path = conclusion.record(tmp_path, _full(), session_id="s1", rounds=1)
    # md 主檔仍落盤、回傳不變。
    assert path == tmp_path / "CONCLUSION.md"
    assert path.is_file()
    # sidecar 不存在、且無殘留 tmp。
    assert not (tmp_path / "conclusion.json").exists()
    assert list(tmp_path.glob("*.tmp")) == []


def test_render_markdown_pure_filters_blank_items():
    """render 純函式：剝空白、跳過空字串條目。"""
    md = conclusion.render_markdown(
        {
            "consensus": ["  x  ", "", "   "],
            "disagreements": [],
            "open_questions": [],
            "actions": [],
        }
    )
    assert "- x" in md
    # 空字串不產生空 bullet；該段只有一條真實內容。
    assert md.count("- ") >= 1
    bullets = [ln for ln in md.splitlines() if ln.startswith("- ")]
    assert "- " not in bullets  # 無空 bullet
