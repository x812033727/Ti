import pytest

from studio.discussion import parse_mentions
from studio.flow import (
    OMITTED_LINE_TEMPLATE,
    compress_segment,
    critic_blocks,
    parse_agenda,
    parse_appraisals,
    parse_ballot,
    parse_clarify,
    parse_conclusion,
    parse_core_changes,
    parse_dispatch,
    parse_followups,
    parse_followups_meta,
    parse_lessons,
    parse_next_step,
    parse_structured_tasks,
    parse_tasks,
    parse_tasks_with_deps,
    parse_vision,
    parse_vote_request,
    pm_done,
    qa_passed,
    security_approved,
    senior_approved,
)


# ① allowlist 與 flow/discussion 全部 parser 一一對應防漂移
def test_allowlist_parser_mapping():
    from studio.flow import _MARKER_LABELS

    expected_labels = {
        "驗證",
        "決議",
        "異議",
        "任務",
        "依賴",
        "問題",
        "假設",
        "澄清",
        "後續任務",
        "核心改動",
        "教訓",
        "願景",
        "共識",
        "分歧",
        "未決",
        "行動",
        "子題",
        "負責",
        "下一步",
        "指示",
        "招募",
        "provider",
        "模型",
        "派工",
        "表決",
        "投票",
        "考核",
    }
    assert set(_MARKER_LABELS) == expected_labels


# ② 白樣本斷言壓縮後 qa_passed/senior_approved/parse_mentions/parse_core_changes 解析結果與原文一致，樣本必含行中裁決行與行中 回應 @
def test_white_sample_compression():
    original_text = (
        "這是一段很長很長的敘述，必須要超過 cap 的預算限制以觸發壓縮。\n"
        "我們在這裡放一些廢話，這些廢話應該被壓縮器省去，以騰出預算。\n"
        "這裡又有一行廢話。\n"
        "測試 驗證: PASS 這裡是行中驗證\n"
        "討論 決議: 核可 這裡是行中決議\n"
        "大家來 回應 @engineer: 同意 很好 這裡是行中回應\n"
        "變更 核心改動:\n"
        "- 修改了 discussion.py 以支援 compress_segment\n"
        "結尾再加一行廢話，總長度一定要超過 200 字元。"
    )
    cap = 150
    compressed = compress_segment(original_text, cap)

    # 斷言壓縮後結果與原文一致
    assert qa_passed(compressed) == qa_passed(original_text)
    assert senior_approved(compressed) == senior_approved(original_text)

    speaker = "qa"
    participants = ["qa", "engineer"]
    assert parse_mentions(speaker, compressed, participants) == parse_mentions(
        speaker, original_text, participants
    )
    assert parse_core_changes(compressed) == parse_core_changes(original_text)


# ③ 黑樣本以破壞版壓縮器 monkeypatch studio.flow.compress_segment 證明測試具真判別力
def test_black_sample_fails_assertions(monkeypatch):
    def run_assertions(text_compressed, text_original):
        assert qa_passed(text_compressed) == qa_passed(text_original)
        assert senior_approved(text_compressed) == senior_approved(text_original)
        assert parse_mentions("qa", text_compressed, ["qa", "engineer"]) == parse_mentions(
            "qa", text_original, ["qa", "engineer"]
        )
        assert parse_core_changes(text_compressed) == parse_core_changes(text_original)

    original_text = (
        "這是一段很長很長的敘述，必須要超過 cap 的預算限制以觸發壓縮。\n"
        "我們在這裡放一些廢話，這些廢話應該被壓縮器省去，以騰出預算。\n"
        "測試 驗證: PASS 這裡是行中驗證\n"
        "討論 決議: 核可 這裡是行中決議\n"
        "大家來 回應 @engineer: 同意 很好 這裡是行中回應\n"
        "變更 核心改動:\n"
        "- 修改了 discussion.py\n"
    )

    # 正常的壓縮器，執行 assertions 應該成功
    compressed_good = compress_segment(original_text, 150)
    run_assertions(compressed_good, original_text)

    # 破壞版壓縮器：會吃掉 marker
    def broken_compress(text, cap):
        lines = text.splitlines(keepends=True)
        from studio.flow import _MARKER_RE

        return "".join(line for line in lines if not _MARKER_RE.search(line))

    monkeypatch.setattr("studio.flow.compress_segment", broken_compress)

    compressed_bad = broken_compress(original_text, 150)

    # 我們斷言跑同組 assertions 必定會引發 AssertionError！
    with pytest.raises(AssertionError):
        run_assertions(compressed_bad, original_text)


# ④ 不變式測試：標頭與省略標記行餵全部 parser + parse_mentions 斷言零命中
def test_invariants_no_parser_hits():
    # 標頭樣式
    header_qa = "以下為 @qa 發言之摘要（結構化行為原文保留）"
    header_eng = "以下為 @engineer 發言之摘要（結構化行為原文保留）"
    header_senior = "以下為 @senior 發言之摘要（結構化行為原文保留）"
    header_pm = "以下為 @pm 發言之摘要（結構化行為原文保留）"

    # 省略標記樣式
    omitted_1 = OMITTED_LINE_TEMPLATE.format(n=1)
    omitted_5 = OMITTED_LINE_TEMPLATE.format(n=5)

    test_cases = [header_qa, header_eng, header_senior, header_pm, omitted_1, omitted_5]

    # 收集所有的 parser 及其預期的無命中預設回傳值
    parsers_bool = {
        qa_passed: True,  # 預設為 True (無 fail 詞)
        senior_approved: True,  # 預設為 True (無 退回 詞)
        security_approved: True,  # 預設為 True (無 漏洞 詞)
        critic_blocks: False,  # 預設為 False (無 異議 詞)
        pm_done: False,  # 預設為 False (無 完成 詞)
    }
    parsers_list = [
        parse_tasks,
        parse_clarify,
        parse_structured_tasks,
        parse_followups,
        parse_followups_meta,
        parse_core_changes,
        parse_lessons,
        parse_agenda,
        parse_appraisals,
    ]
    parsers_dict = [parse_next_step, parse_dispatch, parse_conclusion]

    for case in test_cases:
        # bool parsers 預期為預設值
        for p, expected in parsers_bool.items():
            assert p(case) is expected, (
                f"Parser {p.__name__} unexpectedly returned {p(case)} instead of {expected} for case '{case}'"
            )

        # list parsers 預期為空 list
        for p in parsers_list:
            res = p(case)
            if p == parse_tasks:
                assert res == ["實作需求"], (
                    f"Parser {p.__name__} unexpectedly matched '{case}' with result {res}"
                )
            elif p == parse_structured_tasks:
                assert res == [{"title": "實作需求", "priority": 1, "type": "improvement"}], (
                    f"Parser {p.__name__} unexpectedly matched '{case}' with result {res}"
                )
            elif p == parse_agenda:
                assert res == [
                    {"title": case.strip(), "description": "", "criteria": "", "assignee": ""}
                ], f"Parser {p.__name__} unexpectedly matched '{case}' with result {res}"
            else:
                assert isinstance(res, list) and len(res) == 0, (
                    f"Parser {p.__name__} unexpectedly matched '{case}' with result {res}"
                )

        # dict parsers 預期為空 dict
        for p in parsers_dict:
            res = p(case)
            if p == parse_conclusion:
                assert all(len(v) == 0 for v in res.values()), (
                    f"Parser {p.__name__} unexpectedly matched '{case}' with result {res}"
                )
            elif p == parse_next_step:
                assert res == {
                    "role": "",
                    "instruction": "",
                    "end": False,
                    "recruit": None,
                    "provider": "",
                    "model": "",
                }
            else:
                assert isinstance(res, dict) and len(res) == 0, (
                    f"Parser {p.__name__} unexpectedly matched '{case}' with result {res}"
                )

        # 其他特殊回傳值的 parser
        assert parse_vision(case) == "", f"parse_vision unexpectedly matched '{case}'"
        assert parse_ballot(case, ["A", "B"]) == "", f"parse_ballot unexpectedly matched '{case}'"
        tasks, deps = parse_tasks_with_deps(case)
        assert tasks == [{"id": 1, "title": "實作需求", "status": "todo"}] and len(deps) == 0, (
            f"parse_tasks_with_deps unexpectedly matched '{case}' with result {tasks}, {deps}"
        )
        assert parse_vote_request(case) is None, f"parse_vote_request unexpectedly matched '{case}'"

        # parse_mentions 回傳 list
        res_mentions = parse_mentions("qa", case, ["qa", "engineer", "senior", "pm"])
        assert len(res_mentions) == 0, (
            f"parse_mentions unexpectedly matched '{case}' with result {res_mentions}"
        )
