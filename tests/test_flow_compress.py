"""黑白樣本單元測試：守護 ``studio.flow.compress_segment`` 的裁決保真（任務 #3）。

白樣本（4 parser）：構造 ``len(text) > cap`` 的輸入，斷言
``parser(compress_segment(text, cap)) == parser(text)``——證明壓縮後四個解析器
（``qa_passed``／``senior_approved``／``parse_mentions``／``parse_core_changes``）
的結果與原文**完全一致**（筆數、順序、標籤、後備分支判定都不變）。

黑樣本（破壞版）：以「模仿 ``discussion._clip`` 舊式『從頭截尾保結尾』粗暴截法」
的尾截壓縮器對照，斷言破壞版必被攔下（裁決行被吃 → 解析結果與原文不一致）
——證明測試有真判別力：若改成「怎樣都綠」即假綠。

守護測試：
1. ``orchestrator.compress_segment is flow.compress_segment``（re-export 不漂移）
2. ``OMITTED_LINE_TEMPLATE`` 本身不含任何 marker 子串／後備關鍵詞／fake_experts
   ``action_marker``（壓縮輸出餵四個 parser 必須零命中——避免「壓縮輸出反而被
   解析成裁決」）。
3. ``MARKER_ALLOWLIST`` 與 flow.py / discussion.py 全部 parser 一一對應防漂移
   （漏一個 marker → 該 marker 行在壓縮時被當敘述 → 裁決可能被吞）。
"""

from __future__ import annotations

import pytest

from studio import discussion, flow, orchestrator

# ===== 守護測試（re-export + 標頭/標記乾淨 + allowlist 全覆蓋）=============


def test_guard_orchestrator_reexports_compress_segment():
    """re-export 守護：orchestrator.compress_segment 必須是 flow.compress_segment 本身，
    供既有 `monkeypatch.setattr(studio.orchestrator, 'compress_segment', ...)` 慣例。
    行為漂移會讓 autopilot/improver 既有的 monkeypatch 測試集體失效。"""
    assert orchestrator.compress_segment is flow.compress_segment


def test_guard_omitted_line_template_is_marker_clean():
    """OMITTED_LINE_TEMPLATE 必須乾淨：
    - 不含 ``_MARKER_RE`` 任何 marker pattern（標籤+冒號／``回應 @``）
    - 不含 ``_FALLBACK_VERDICTS`` 任一後備關鍵詞（防誤觸 qa_passed／senior_approved
      ／security_approved／critic_blocks／pm_done 的後備分支）
    - 不含 ``fake_experts`` 的 ``action_marker``（防假專家誤觸「任務 #」寫檔）
    並把模板餵入四個 parser 斷言零命中——避免「壓縮輸出反而被解析成裁決」。"""
    template = flow.OMITTED_LINE_TEMPLATE
    # 對齊模板格式化（傳入 n=5 展開 placeholder）：守護測的是「實際寫入輸出的那行」。
    rendered = template.format(n=5)
    marker_re = flow._MARKER_RE
    assert not marker_re.search(rendered), (
        f"OMITTED_LINE_TEMPLATE 命中 marker pattern: {rendered!r}"
    )
    for _, fallback_re in flow._FALLBACK_VERDICTS:
        assert not fallback_re.search(rendered), (
            f"OMITTED_LINE_TEMPLATE 命中後備關鍵詞 {fallback_re.pattern!r}: {rendered!r}"
        )
    for action_marker in ("任務 #", "撰寫並執行測試"):
        assert action_marker not in rendered, (
            f"OMITTED_LINE_TEMPLATE 碰撞 fake_experts action_marker {action_marker!r}"
        )
    # 四個 parser 各自餵入單行模板，斷言零命中
    assert flow.qa_passed(rendered) is True
    assert flow.senior_approved(rendered) is True
    assert discussion.parse_mentions("甲", rendered, ["乙", "丙"]) == []
    assert flow.parse_core_changes(rendered) == []


def test_guard_marker_allowlist_covers_all_parsers():
    """MARKER_ALLOWLIST 與 flow.py / discussion.py 全部 parser 一一對應防漂移。
    漏一個 marker → 該 marker 行在壓縮時被當敘述 → 裁決可能被吞。"""
    allowlist_joined = "|".join(flow.MARKER_ALLOWLIST)
    # flow.py parser 對應的標籤（鏡射 _MARKER_LABELS 與守護註解的對應表）
    expected_labels = [
        "驗證",  # qa_passed
        "決議",  # senior_approved / security_approved / pm_done
        "異議",  # critic_blocks
        "任務",  # parse_tasks / parse_tasks_with_deps / _RE_TAGGED_TASK
        "依賴",  # parse_tasks_with_deps
        "問題",  # parse_clarify
        "假設",  # parse_clarify
        "澄清",  # parse_clarify
        "後續任務",  # _RE_TAGGED_FOLLOWUP / parse_followups_meta
        "核心改動",  # _RE_CORE_CHANGE / parse_core_changes
        "教訓",  # parse_lessons
        "願景",  # parse_vision
        "共識",  # parse_conclusion
        "分歧",  # parse_conclusion
        "未決",  # parse_conclusion
        "行動",  # parse_conclusion
        "子題",  # parse_agenda
        "負責",  # parse_agenda
        "下一步",  # parse_next_step
        "指示",  # parse_next_step
        "招募",  # parse_next_step
        "provider",  # parse_next_step
        "模型",  # parse_next_step
        "派工",  # parse_dispatch
        "表決",  # parse_vote_request
        "投票",  # parse_ballot
        "考核",  # parse_appraisals
    ]
    for label in expected_labels:
        assert label in allowlist_joined, (
            f"MARKER_ALLOWLIST 漏掉 parser 標籤 {label!r}（該 parser 的行會被當敘述壓掉）"
        )
    # discussion.parse_mentions 的「回應 @」結構化引用（不是純標籤+冒號）
    assert any("回應" in p and "@" in p for p in flow.MARKER_ALLOWLIST), (
        "MARKER_ALLOWLIST 漏掉 discussion.parse_mentions 的「回應 @」結構化引用"
    )


# ===== 共用構造工具（敘述 padding，避免污染 fallback 關鍵詞 / action_marker）====


def _pad(line: str, width: int) -> str:
    """把行 padding 到 width，僅用安全字元（避開 fallback 關鍵詞、action_marker）。

    安全字元選用 ASCII 字母：既不會命中 ``\\b(fail|failed|error|錯誤|失敗)\\b``
    後備分支，也不會撞 fake_experts 的「任務 #」／「撰寫並執行測試」。
    """
    return line + "x" * max(0, width - len(line))


def _long_narrative(width: int = 80, lines: int = 6) -> str:
    """回傳多行安全敘述（不含 marker、不含後備關鍵詞、不含 action_marker）。"""
    return "\n".join(_pad(f"敘述行{i}", width) for i in range(lines))


# ===== 白樣本：parser(compress(text)) == parser(text) ========================


def test_white_short_text_returns_bit_for_bit():
    """白樣本（防壓縮悖論）：``len(text) <= cap`` 時 bit-for-bit 原樣返回。"""
    text = "驗證: PASS\n決議: 核可\n核心改動: [P0/bug] 修波次死結\n回應 @高級工程師: 同意 ＋同意\n"
    assert len(text) <= 1000
    assert flow.compress_segment(text, 1000) == text
    # cap 等於長度也算「不壓」
    assert flow.compress_segment(text, len(text)) == text


def test_white_all_marker_lines_preserved_bit_for_bit():
    """白樣本（結構性）：壓縮後每個 marker 行必須逐字元原文存在、相對順序不變。
    ``_last_match`` 取最後一筆——順序改變 = 判定翻盤，故相對順序亦為不變式。"""
    narrative = _long_narrative(width=80, lines=6)
    text = (
        "驗證: PASS\n"
        + narrative
        + "\n"
        + "決議: 核可\n"
        + narrative
        + "\n"
        + "核心改動: [P0/bug] 修波次死結\n"
        + narrative
        + "\n"
        + "回應 @高級工程師: 同意 ＋同意\n"
        + narrative
        + "\n"
    )
    cap = 400
    assert len(text) > cap

    compressed = flow.compress_segment(text, cap)
    marker_lines = [
        "驗證: PASS",
        "決議: 核可",
        "核心改動: [P0/bug] 修波次死結",
        "回應 @高級工程師: 同意 ＋同意",
    ]
    for line in marker_lines:
        assert line in compressed, f"marker 行 {line!r} 必須原文保留"
    # 相對順序：原文順序 == 壓縮輸出順序
    positions = [compressed.index(line) for line in marker_lines]
    assert positions == sorted(positions), (
        f"marker 行相對順序不可改變（_last_match 翻盤風險）：{positions}"
    )


def test_white_qa_passed_consistent_when_over_cap():
    """白樣本：含 ``驗證: PASS`` 在中段；敘述行安全 padding（不含 ``fail|failed|error|
    錯誤|失敗``）。壓縮後 qa_passed 與原文一致——證明 marker 行被保留且敘述被丟不會
    翻轉裁決。"""
    narrative = _long_narrative(width=80, lines=6)
    text = (
        narrative
        + "\n"
        + narrative
        + "\n"
        + "驗證: PASS\n"
        + narrative
        + "\n"
        + narrative
        + "\n"
        + narrative
        + "\n"
    )
    cap = 300
    assert len(text) > cap

    assert flow.qa_passed(text) is True
    compressed = flow.compress_segment(text, cap)
    assert "驗證: PASS" in compressed
    assert flow.qa_passed(compressed) is True


def test_white_senior_approved_consistent_when_over_cap():
    """白樣本：含 ``決議: 核可`` 在中段；敘述不含 senior 後備關鍵詞。"""
    narrative = _long_narrative(width=80, lines=6)
    text = (
        narrative
        + "\n"
        + narrative
        + "\n"
        + "決議: 核可\n"
        + narrative
        + "\n"
        + narrative
        + "\n"
        + narrative
        + "\n"
    )
    cap = 300
    assert len(text) > cap

    assert flow.senior_approved(text) is True
    compressed = flow.compress_segment(text, cap)
    assert "決議: 核可" in compressed
    assert flow.senior_approved(compressed) is True


def test_white_senior_approved_fallback_branch_consistent_when_over_cap():
    """白樣本（後備分支）：全文**無** ``決議:`` 顯式 marker，只有後備關鍵詞行
    （含「退回」「必須修正」）。senior_approved 走 fallback（``re.search`` 全文）。
    壓縮後 fallback 結果仍應一致——證明 ``_FALLBACK_VERDICTS`` 防護把後備關鍵詞行
    整行原文保留，避免「敘述行被吃光、後備關鍵詞湊巧不在壓縮段」造成假放行。

    這是審查實測的**假放行**最容易發生的路徑：fallback 全文掃描，敘述行一旦被
    壓縮掉就可能翻轉判定。"""
    # 注意：此樣本刻意把後備關鍵詞行放在「中段」（非保頭也非保尾），純粹靠 allowlist
    # 後備機制保留；若 allowlist 沒帶 _FALLBACK_VERDICTS 防護，敘述行被丟、關鍵詞行
    # 也被當敘述丟，senior_approved 會從 False 翻 True（假放行）。
    text = (
        _long_narrative(width=80, lines=3)
        + "\n"
        + _pad("這段結論必須退回修正，依規走退回流程", 80)
        + "\n"  # 後備命中「退回」「必須修正」
        + _long_narrative(width=80, lines=5)
        + "\n"
        + _pad("結尾備註：仍建議退回", 80)
        + "\n"  # 後備命中「退回」
    )
    cap = 300
    assert len(text) > cap

    assert flow.senior_approved(text) is False  # 原文：fallback 命中「退回」
    compressed = flow.compress_segment(text, cap)
    assert "必須退回修正" in compressed, "後備關鍵詞行必須原文保留"
    assert "仍建議退回" in compressed
    assert flow.senior_approved(compressed) is False


def test_white_parse_mentions_consistent_when_over_cap_with_inline():
    """白樣本：含多個 ``回應 @角色名: 同意/反對``，**含行中版本**（不錨定行首）；
    壓縮後筆數與順序與原文完全一致——直接斷言 dataclass list 相等。

    行中版本覆蓋「裁決 regex 行中可命中」的設計決策：marker 行**全文掃描**而非
    ``^`` 錨定，allowlist 亦鏡射此語意。光測行首樣本不足以驗此不變式。"""
    participants = ("工程師", "高級工程師", "架構師")
    narrative = _long_narrative(width=100, lines=8)
    text = (
        narrative
        + "\n"
        + "回應 @架構師: 同意 ＋理由A\n"
        + narrative
        + "\n"
        + narrative
        + "\n"
        + "回應 @高級工程師: 反對 ＋理由B\n"
        + narrative
        + "\n"
        + "行中測試：根據上下文需要再補充「回應 @工程師: 同意 ＋理由C」這類引用。\n"
        + narrative
        + "\n"
        + narrative
        + "\n"
    )
    cap = 500
    assert len(text) > cap

    orig = discussion.parse_mentions("主持人", text, participants)
    compressed = discussion.parse_mentions("主持人", flow.compress_segment(text, cap), participants)
    assert len(orig) == len(compressed) == 3
    assert orig == compressed  # 完整 dataclass list 相等（speaker / target / stance）
    targets = [(m.target, m.stance) for m in compressed]
    assert ("架構師", "同意") in targets
    assert ("高級工程師", "反對") in targets
    assert ("工程師", "同意") in targets  # 行中版本


def test_white_parse_core_changes_consistent_when_over_cap():
    """白樣本：含多個 ``核心改動: [P0/bug] ...`` 行（含標籤），壓縮後筆數、title、
    priority、type 與順序完全一致。"""
    narrative = _long_narrative(width=100, lines=6)
    text = (
        narrative
        + "\n"
        + narrative
        + "\n"
        + "核心改動: [P0/bug] 修 orchestrator 波次死結\n"
        + narrative
        + "\n"
        + narrative
        + "\n"
        + "核心改動: [feature] runner 加沙箱白名單\n"
        + narrative
        + "\n"
        + "核心改動: [P2] 美化發佈訊息\n"
        + narrative
        + "\n"
        + narrative
        + "\n"
    )
    cap = 500
    assert len(text) > cap

    orig = flow.parse_core_changes(text)
    compressed = flow.parse_core_changes(flow.compress_segment(text, cap))
    assert len(orig) == len(compressed) == 3
    assert orig == compressed
    assert [(t["title"], t["priority"], t["type"]) for t in compressed] == [
        ("修 orchestrator 波次死結", 0, "bug"),
        ("runner 加沙箱白名單", 1, "feature"),
        ("美化發佈訊息", 2, "improvement"),
    ]


# ===== 黑樣本：破壞版（尾截 _clip 形式）必被攔下 ============================


def _tail_clip_break(text: str, cap: int) -> str:
    """破壞版壓縮器：模仿 ``discussion._clip`` 的「從頭截尾保留結尾」舊式粗暴截法。

    正是任務 #1 之前 ``_build_prompt`` 的真實行為——把 marker 行整段截掉、敘述中段
    的裁決 / 結構化引用被吞，導致 qa_passed / senior_approved / parse_mentions /
    parse_core_changes 翻盤。本測試以它當對照組，證明測試有真判別力：若破壞版也
    全綠 = 假綠。"""
    text = (text or "").strip()
    if len(text) <= cap:
        return text
    return "…（前段截斷）" + text[-cap:]


def _assert_parsers_consistent_with_current_compressor(
    text: str,
    cap: int,
    participants: tuple[str, ...],
) -> None:
    """經由 flow.compress_segment 屬性呼叫，讓 monkeypatch 一定打得到。"""
    compressed = flow.compress_segment(text, cap)
    assert flow.qa_passed(compressed) == flow.qa_passed(text)
    assert flow.senior_approved(compressed) == flow.senior_approved(text)
    assert discussion.parse_mentions("主持人", compressed, participants) == (
        discussion.parse_mentions("主持人", text, participants)
    )
    assert flow.parse_core_changes(compressed) == flow.parse_core_changes(text)


def test_black_monkeypatch_breaks_white_sample(monkeypatch):
    """黑樣本（monkeypatch）：把 flow.compress_segment 換成會吃裁決行的破壞版，
    同一組白樣本解析一致性斷言必須失敗，避免「直呼對照組」被誤判為假綠。"""
    participants = ("工程師", "高級工程師", "架構師")
    narrative = _long_narrative(width=100, lines=10)
    text = (
        "驗證: PASS\n"
        "決議: 核可\n"
        "回應 @架構師: 同意 ＋理由A\n"
        "核心改動: [P0/bug] 修 monkeypatch 黑樣本\n"
        + narrative
        + "\n"
        + narrative
        + "\n"
        + _pad("尾段紀錄 failed 且必須退回修正", 100)
        + "\n"
    )
    cap = 250
    assert len(text) > cap

    _assert_parsers_consistent_with_current_compressor(text, cap, participants)

    monkeypatch.setattr(flow, "compress_segment", _tail_clip_break)
    with pytest.raises(AssertionError):
        _assert_parsers_consistent_with_current_compressor(text, cap, participants)


def test_black_tail_clip_drops_decision_line_must_be_caught():
    """黑樣本（senior_approved）：``決議: 核可`` 放開頭（會被尾截吃）、結尾保留
    一行含後備關鍵詞「退回」「必須修正」。

    - 原文：marker 命中 → True（核可）
    - 破壞版尾截：marker 丟、敘述保留 → fallback 命中「退回」→ False（**假退回**）
    - compress_segment：marker 原文保留 → True

    證明測試有真判別力：若改「怎樣都 True」即假綠。"""
    narrative = _long_narrative(width=80, lines=6)
    text = (
        "決議: 核可\n"  # 開頭 marker → 尾截會吃
        + narrative
        + "\n"
        + narrative
        + "\n"
        + narrative
        + "\n"
        + narrative
        + "\n"
        + narrative
        + "\n"
        + narrative
        + "\n"
        + narrative
        + "\n"
        + _pad("這段必須退回修正", 80)
        + "\n"  # 結尾保留後備關鍵詞行
    )
    cap = 250
    assert len(text) > cap

    # 前置：原文 → True；破壞版 → False（marker 丟、後備命中「退回」）
    assert flow.senior_approved(text) is True
    broken = _tail_clip_break(text, cap)
    assert "決議: 核可" not in broken, "破壞版尾截理應吃掉開頭決議行"
    assert "必須退回修正" in broken, "破壞版保留結尾敘述、後備關鍵詞在內"
    assert flow.senior_approved(broken) is False, (
        "破壞版 fallback 命中「退回」「必須修正」→ 假退回（翻 False）"
    )

    # 白樣本正常路徑：compress_segment 保留 marker → 仍 True
    compressed = flow.compress_segment(text, cap)
    assert "決議: 核可" in compressed
    assert flow.senior_approved(compressed) is True


def test_black_tail_clip_drops_qa_marker_must_be_caught():
    """黑樣本（qa_passed）：``驗證: PASS`` 開頭、結尾敘述含後備關鍵詞「failed」。
    破壞版：marker 丟 → fallback 命中「failed」→ 翻 False（**假 FAIL**）；
    compress_segment：marker 保留 → True。"""
    narrative = _long_narrative(width=80, lines=6)
    text = (
        "驗證: PASS\n"  # 開頭 marker → 尾截會吃
        + narrative
        + "\n"
        + narrative
        + "\n"
        + narrative
        + "\n"
        + narrative
        + "\n"
        + narrative
        + "\n"
        + narrative
        + "\n"
        + narrative
        + "\n"
        + _pad("此測試紀錄為 failed 狀態", 80)
        + "\n"  # 結尾含「failed」
    )
    cap = 250
    assert len(text) > cap

    assert flow.qa_passed(text) is True
    broken = _tail_clip_break(text, cap)
    assert "驗證: PASS" not in broken
    assert "failed" in broken
    assert flow.qa_passed(broken) is False, "破壞版 fallback 命中「failed」→ 假 FAIL"

    compressed = flow.compress_segment(text, cap)
    assert "驗證: PASS" in compressed
    assert flow.qa_passed(compressed) is True


def test_black_tail_clip_drops_mention_must_be_caught():
    """黑樣本（parse_mentions）：開頭 ``回應 @架構師: 同意`` 會被尾截吃，結尾保留
    一筆 ``回應 @高級工程師: 反對``。
    破壞版：少一筆（1 vs 2）；compress_segment：保留全部（2）。"""
    participants = ("工程師", "高級工程師", "架構師")
    narrative = _long_narrative(width=80, lines=6)
    text = (
        "回應 @架構師: 同意 ＋理由A\n"  # 開頭 → 尾截會吃
        + narrative
        + "\n"
        + narrative
        + "\n"
        + narrative
        + "\n"
        + narrative
        + "\n"
        + narrative
        + "\n"
        + narrative
        + "\n"
        + narrative
        + "\n"
        + "回應 @高級工程師: 反對 ＋理由B\n"  # 結尾 → 尾截會保留
    )
    cap = 250
    assert len(text) > cap

    orig = discussion.parse_mentions("主持人", text, participants)
    assert len(orig) == 2

    broken = discussion.parse_mentions("主持人", _tail_clip_break(text, cap), participants)
    assert len(broken) == 1, "破壞版少一筆結構化引用（開頭 mention 被吃）"
    assert broken[0].target == "高級工程師"

    compressed = discussion.parse_mentions("主持人", flow.compress_segment(text, cap), participants)
    assert len(compressed) == 2, "compress_segment 應保留全部結構化引用"
    assert compressed == orig


def test_black_tail_clip_drops_core_change_must_be_caught():
    """黑樣本（parse_core_changes）：開頭 ``核心改動: [P0/bug] ...`` 被尾截吃，
    結尾保留一筆 ``核心改動: [feature] ...``。
    破壞版：少一筆（1 vs 2）；compress_segment：保留全部（2）。"""
    narrative = _long_narrative(width=80, lines=6)
    text = (
        "核心改動: [P0/bug] 修 orchestrator 波次死結\n"  # 開頭 → 尾截會吃
        + narrative
        + "\n"
        + narrative
        + "\n"
        + narrative
        + "\n"
        + narrative
        + "\n"
        + narrative
        + "\n"
        + narrative
        + "\n"
        + narrative
        + "\n"
        + "核心改動: [feature] runner 加沙箱白名單\n"  # 結尾 → 尾截會保留
    )
    cap = 250
    assert len(text) > cap

    orig = flow.parse_core_changes(text)
    assert len(orig) == 2

    broken = flow.parse_core_changes(_tail_clip_break(text, cap))
    assert len(broken) == 1, "破壞版少一筆核心改動（開頭被吃）"
    assert broken[0]["title"] == "runner 加沙箱白名單"

    compressed = flow.parse_core_changes(flow.compress_segment(text, cap))
    assert len(compressed) == 2, "compress_segment 應保留全部核心改動"
    assert compressed == orig
