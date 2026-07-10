"""discovered followup 的「良構性/價值閘」（完成率第二輪修法 ①）。

背景（完成率第二輪診斷）：前輪三大失敗桶（討論不收斂/lint/零-diff）皆為症狀，真正上游是 autopilot
自我衍生的「收尾驗收/權威證據檔/closure 報告/sha256 落檔/重跑並回報」這類**無會改動程式碼的客觀完成
判準**的自我指涉 meta 任務——它們同時灌三桶（ill-posed 討論永不收斂、生成檔 from __future__/E402 lint
修不掉、零-diff merge）。F（#362）的去重防線只擋「重複」，擋不掉「全新但同樣沒價值」的 busywork。

本閘（`_is_low_value_followup`）是 F 的延伸：命中「證據儀式」busywork 訊號 **AND** 缺任一 code-work
（實作/修復/測試/守門）訊號才丟棄，刻意保守偏向「寧放勿殺」。樣本取自 live backlog 真實失敗/pending 標題。

純正則 + monkeypatch，不打 LLM。
"""

from __future__ import annotations

import pytest

from studio import autopilot, config

# 取自 live backlog 的真實低價值 meta 提案（應丟棄）：純落檔/純重跑回報/純寫慣例/純產 evidence。
_BUSYWORK = [
    "綜合 #1#2 產出「一句話歸屬結論＋雙邊證據行號」權威判定，落 $TMPDIR 並附 sha256、唯一權威聲明",
    "以新時間戳後綴檔落盤「權威證據檔宣告」：指定 jpuOxQ.log 為唯一權威、其餘四檔明文作廢",
    "補交任務 #3 落盤複核決議（含決議檔名與 sha256）",
    "以帶時間戳＋不可預測後綴的新檔名重跑收尾驗收單一 QA pass，落檔即回報 sha256 供 PM 判定",
    "由 QA 重跑收尾驗收單一 pass，四段結構重新落檔 $TMPDIR，落檔後立即 chmod a-w 凍結並出 sha256sum",
    "執行收尾驗收單一 QA pass：依序跑 pytest tests/docs -q",
    "收尾驗收：跑 pytest tests/docs -q 全綠，git diff docs/ 確認乾淨",
    "將 #1/#2 重驗結果更新進 docs/release-e2e-closure-report.md",
    "重跑證據 #1/#2 線上重驗指令",
    "重跑全量 pytest -q 並僅以回報訊息附上結果，確認 git status 乾淨後由 PM 複驗收尾",
    "在 evidence JSON 補存 release-smoke run 的關鍵 log 摘要",
    "將 body_sha256 的含 jq 尾換行算法定義補進 evidence 慣例說明",
    "將閉環報告的逐項比對轉錄模板抽成 docs/ 內可重用範本",
    "下一個 v* tag 發佈後實跑 handoff 步驟 B 並回填該版線上 body／smoke evidence",
]

# 取自 live backlog 的真實合法工程任務（不得誤殺）：帶實作/修復/測試/守門等會改碼訊號者一律豁免。
_LEGIT = [
    "lane 合併後過渡段 silent-hang——非 LLM await 缺 timeout 防護",
    "parked 逾時任務拆分再排機制——逾時任務不再無聲死亡",
    "改造 publisher.remote_url() 與 _push/_push_base 路徑改用乾淨 URL＋注入層",
    "實作共用 git 憑證注入層（inline credential helper 或 GIT_ASKPASS）",
    "依延遲量測數據實作一項最高價值的專家通訊改善",
    "讓 orchestrator 在波次中途偵測「已派工但 lane 無任何 commit」並回報 PM",
    "為 handoff doc 邊界表狀態新增守門測試，斷言標 ✅ 的列必須附具名證據檔路徑",  # 守門測試/斷言 豁免
    "讓 backlog/improver 派發修復類任務前先跑一次現況重驗，已失效的過時任務自動下架",  # 修復 豁免
    "將 evidence 的 body_sha256 計算方式瑕疵修復立為獨立任務",  # 修復 豁免
    "讓測試套件在缺 socksio 環境自動 skip test_ws_attach_real_server",  # 無 busywork 訊號
]


@pytest.fixture(autouse=True)
def _gate_on(monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_FOLLOWUP_VALUE_GATE", True)


@pytest.mark.parametrize("title", _BUSYWORK)
def test_busywork_rejected(title):
    assert autopilot._is_low_value_followup(title) is True, f"應判低價值丟棄：{title!r}"


@pytest.mark.parametrize("title", _LEGIT)
def test_legit_work_kept(title):
    assert autopilot._is_low_value_followup(title) is False, f"不得誤殺合法工作：{title!r}"


def test_gate_disabled_passthrough(monkeypatch):
    """關掉旋鈕即恢復舊行為：一律不判低價值（不改 F 的去重契約）。"""
    monkeypatch.setattr(config, "AUTOPILOT_FOLLOWUP_VALUE_GATE", False)
    assert autopilot._is_low_value_followup(_BUSYWORK[0]) is False


def test_detail_rescues_busywork_title():
    """標題像 busywork、但 detail 帶會改碼動詞 → 豁免保留（雙欄位皆納入 code-work 判斷）。"""
    assert autopilot._is_low_value_followup("收尾驗收 QA pass") is True
    assert (
        autopilot._is_low_value_followup(
            "收尾驗收 QA pass", detail="順帶修復 runner.py 的 timeout regression"
        )
        is False
    )


def test_no_signal_is_not_low_value():
    """既無 busywork 也無 code-work 訊號的一般任務不受影響。"""
    assert autopilot._is_low_value_followup("優化前端 timeline 動畫流暢度") is False


def _screen_state(monkeypatch):
    monkeypatch.setattr(autopilot, "_recent_done_titles", lambda: set())


def test_screen_followups_drops_busywork_keeps_real(monkeypatch):
    """進 _screen_followups 端到端：busywork 被第三道價值閘丟棄、真實工作保留，型別/順序不變。"""
    _screen_state(monkeypatch)
    items = [
        {"title": _BUSYWORK[5], "detail": "跑測試", "type": "bug"},
        {"title": _LEGIT[3], "detail": "注入層", "type": "improvement"},
    ]
    out = autopilot._screen_followups(items, [])
    assert [i["title"] for i in out] == [_LEGIT[3]], "只保留真實工作、剔除 busywork"
    assert out[0] == items[1], "保留項的欄位與型別不變"


def test_screen_followups_plain_strings(monkeypatch):
    """純標題字串路徑也走價值閘。"""
    _screen_state(monkeypatch)
    out = autopilot._screen_followups([_BUSYWORK[0], _LEGIT[0]], [])
    assert out == [_LEGIT[0]]


def test_screen_followups_gate_off_keeps_busywork(monkeypatch):
    """關閘 + 無重複 → busywork 照舊放行（證明是本閘在起作用、非其他防線）。"""
    _screen_state(monkeypatch)
    monkeypatch.setattr(config, "AUTOPILOT_FOLLOWUP_VALUE_GATE", False)
    out = autopilot._screen_followups([_BUSYWORK[6]], [])
    assert out == [_BUSYWORK[6]]
