"""QA 驗證：任務 #3 「② 各欄位輸入與驗證」區塊案例。

驗收標準（任務 #3）：
- 每欄至少涵蓋 正常值／空值／邊界值／非法值（高風險欄全展開）。
- select 欄須有「非法選項」案例。
- 須含「超長輸入不撐破版面」案例。
- 案例對齊實際欄位（26 個 select：4 基本＋2 Claude 模型＋8 角色模型＋12 進階組、2 combo、5 文字、3 秘密）。
- 文件宣稱的「後端擋下非法 select」「秘密留空不變更」須為**真實行為**——
  以實際呼叫 settings.update() 佐證。
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / "uat-settings-checklist.md"
HEADER = ["#", "功能區塊", "操作步驟", "預期結果", "實際結果", "Pass/Fail", "備註"]


def _split(row: str) -> list[str]:
    return [c.strip() for c in row.strip().strip("|").split("|")]


def _section(text: str, mark: str, nxt: str) -> str:
    return text.split(f"## {mark}", 1)[1].split(f"## {nxt}", 1)[0]


def _rows(text: str, mark: str, nxt: str) -> list[list[str]]:
    sec = _section(text, mark, nxt)
    out = []
    seen = False
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
def rows(text):
    return _rows(text, "②", "③")


@pytest.fixture(scope="module")
def sec(text):
    return _section(text, "②", "③")


# ---------- 格式 ----------


def test_有實際案例(rows):
    assert len(rows) >= 12, f"② 案例過少：{len(rows)}"


def test_每列七欄關鍵欄非空(rows):
    for r in rows:
        assert len(r) == 7, f"欄數不為 7：{r}"
        assert r[2], f"案例 {r[0]} 缺操作步驟"
        assert r[3], f"案例 {r[0]} 缺預期結果"


def test_編號唯一(rows):
    ids = [r[0] for r in rows]
    assert len(ids) == len(set(ids)), f"編號重複：{ids}"


# ---------- 四象限關鍵字覆蓋 ----------


def test_四象限關鍵字皆出現(sec):
    quad = {"正常": "正常", "空值": "空", "邊界": "邊界", "非法": "非法"}
    missing = [k for k, kw in quad.items() if kw not in sec]
    assert not missing, f"② 缺四象限關鍵字：{missing}"


def test_超長輸入不破版有案例(sec):
    assert "超長" in sec, "缺『超長輸入』案例"
    assert "破版" in sec or "變形" in sec or "溢出" in sec or "捲軸" in sec, (
        "超長案例未描述『不撐破版面』的可觀察預期"
    )


# ---------- select 欄非法選項覆蓋（對齊實際欄位）----------


def test_每個select欄都有非法選項案例(sec):
    """所有 select 欄都應出現在『非法值』語境。"""
    from studio import settings

    selects = [f for f in settings.FIELDS if f.kind == "select"]
    # 用 env 名或 label 關鍵字判斷其非法案例是否存在。基本 select 各自展開；
    # 進階組（0/1 或固定選項）由一條彙整的「非法值一律忽略」案例涵蓋（列名所有 env）。
    checks = {
        "TI_PROVIDER": ["TI_PROVIDER", "Provider"],
        "TI_MODEL_LEAD": ["TI_MODEL_LEAD", "主力模型"],
        "TI_MODEL_FAST": ["TI_MODEL_FAST", "快速模型"],
        # 角色模型（8 欄）：彙整於 2.25 一條案例，逐 env 列名
        "TI_MODEL_PM": ["TI_MODEL_PM"],
        "TI_MODEL_ENGINEER": ["TI_MODEL_ENGINEER"],
        "TI_MODEL_QA": ["TI_MODEL_QA"],
        "TI_MODEL_SENIOR": ["TI_MODEL_SENIOR"],
        "TI_MODEL_RESEARCHER": ["TI_MODEL_RESEARCHER"],
        "TI_MODEL_ARCHITECT": ["TI_MODEL_ARCHITECT"],
        "TI_MODEL_SECURITY": ["TI_MODEL_SECURITY"],
        "TI_MODEL_DEVOPS": ["TI_MODEL_DEVOPS"],
        "TI_PARALLEL_LANES": ["TI_PARALLEL_LANES", "支線數"],
        "TI_PARALLEL_TASKS": ["TI_PARALLEL_TASKS", "任務並行"],
        "TI_PUBLISH_MERGE": ["TI_PUBLISH_MERGE", "自動合併"],
        "TI_CLARIFY": ["TI_CLARIFY"],
        "TI_HUDDLE": ["TI_HUDDLE"],
        "TI_CRITIC": ["TI_CRITIC"],
        "TI_NOTES": ["TI_NOTES"],
        "TI_LESSONS": ["TI_LESSONS"],
        "TI_REFLEXION": ["TI_REFLEXION"],
        "TI_OBJECTIVE_GATE": ["TI_OBJECTIVE_GATE", "閘門"],
        "TI_SELF_REFINE_ITERS": ["TI_SELF_REFINE_ITERS", "自我精修"],
        "TI_RLIMITS": ["TI_RLIMITS", "資源上限"],
        "TI_KNOWLEDGE": ["TI_KNOWLEDGE", "知識沉澱"],
        "TI_BLUEPRINT": ["TI_BLUEPRINT", "產品藍圖"],
        "TI_ADR": ["TI_ADR", "架構決策記錄"],
        "TI_RESEARCH_TOOLS": ["TI_RESEARCH_TOOLS", "即時研究"],
        "TI_DISCUSS_MODE": ["TI_DISCUSS_MODE", "討論模式"],
    }
    assert {f.env for f in selects} == set(checks), "select 欄與預期不符，請更新測試"
    # 非法案例段落：含『非法』字樣的列
    illegal_lines = [ln for ln in sec.splitlines() if "非法" in ln]
    blob = "\n".join(illegal_lines)
    missing = []
    for env, kws in checks.items():
        if not any(k in blob for k in kws):
            missing.append(env)
    assert not missing, f"下列 select 欄缺『非法選項』案例：{missing}"


def test_select邊界值有案例(sec):
    # 並行支線數 1–6 的上下限
    assert "1" in sec and "6" in sec, "缺並行支線數 1/6 邊界案例"


# ---------- 真實行為佐證：後端 update() ----------


@pytest.fixture
def capture_update(monkeypatch):
    """攔截 update() 的寫入，避免污染真實 .env，並記錄哪些 key 真的被寫。"""
    from studio import settings

    written: dict[str, str] = {}

    def fake_write(path, key, val):
        written[key] = val

    monkeypatch.setattr(settings, "write_secret_file", fake_write)
    monkeypatch.setattr(settings.config, "reload", lambda: None)
    monkeypatch.setattr(settings, "read", lambda: {"fields": []})
    return settings, written


def test_後端忽略非法select值(capture_update):
    settings, written = capture_update
    settings.update(
        {
            "TI_PROVIDER": "bogus",  # 非法 → 應忽略
            "TI_PARALLEL_LANES": "0",  # 非法（範圍外）→ 應忽略
            "TI_PARALLEL_TASKS": "2",  # 非法 → 應忽略
            "TI_PUBLISH_MERGE": "9",  # 非法 → 應忽略
        }
    )
    assert written == {}, f"非法 select 值不應被寫入，卻寫了：{written}"


def test_後端接受合法select值(capture_update):
    settings, written = capture_update
    settings.update(
        {
            "TI_PROVIDER": "openai",
            "TI_PARALLEL_LANES": "6",
            "TI_PARALLEL_TASKS": "1",
            "TI_PUBLISH_MERGE": "0",
        }
    )
    assert written == {
        "TI_PROVIDER": "openai",
        "TI_PARALLEL_LANES": "6",
        "TI_PARALLEL_TASKS": "1",
        "TI_PUBLISH_MERGE": "0",
    }, f"合法 select 值應全部寫入，實際：{written}"


def test_後端秘密留空不變更(capture_update):
    settings, written = capture_update
    settings.update({"ANTHROPIC_API_KEY": "", "OPENAI_API_KEY": "   "})
    assert written == {}, f"秘密欄留空（含純空白）不應寫入：{written}"


def test_後端文字與combo欄接受任意值含超長(capture_update):
    settings, written = capture_update
    longv = "x" * 2000
    settings.update({"TI_OPENAI_MODEL_LEAD": longv, "TI_PUBLISH_REPO": "owner/repo"})
    assert written["TI_OPENAI_MODEL_LEAD"] == longv, (
        "combo 欄應接受任意值含超長（後端不限長、不套白名單）"
    )
    assert written["TI_PUBLISH_REPO"] == "owner/repo"


def test_後端擋下Claude模型欄非法值(capture_update):
    settings, written = capture_update
    settings.update({"TI_MODEL_LEAD": "bogus-model", "TI_MODEL_FAST": "gpt-4o"})
    assert written == {}, f"清單外的 Claude 模型值不應被寫入，卻寫了：{written}"


def test_後端接受Claude模型欄合法值(capture_update):
    settings, written = capture_update
    settings.update({"TI_MODEL_LEAD": "claude-opus-4-8", "TI_MODEL_FAST": "claude-haiku-4-5"})
    assert written == {
        "TI_MODEL_LEAD": "claude-opus-4-8",
        "TI_MODEL_FAST": "claude-haiku-4-5",
    }, f"合法 Claude 模型值應全部寫入，實際：{written}"


def test_後端忽略白名單外的鍵(capture_update):
    settings, written = capture_update
    settings.update({"EVIL_KEY": "x", "PATH": "/tmp"})
    assert written == {}, f"白名單外的鍵不應被寫入：{written}"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
