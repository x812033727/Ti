"""產品藍圖模組的單元測試（純檔案 IO，不需 LLM）。"""

from __future__ import annotations

import pytest

from studio import backlog, blueprint, config, projects

SAMPLE = """前置說明文字，應被忽略。
願景: 讓小型農場用無人機自動巡田
用戶: 自有 1~10 公頃田地的小農
功能: [P0] 航線規劃 — 在地圖上圈選範圍自動產生巡航路徑
功能: [P0] 即時影像串流
功能: [P1] 異常偵測 — 影像辨識病蟲害與缺水區塊
功能: 報表匯出 — 巡田結果輸出 PDF
功能: [P2] 多機協同
里程碑: M1 單機手動航線 + 影像串流
里程碑: M2 自動異常偵測
"""


@pytest.fixture
def proj(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PROJECTS_ROOT", tmp_path / "projects")
    monkeypatch.setattr(config, "WORKSPACE_ROOT", tmp_path / "workspaces")
    monkeypatch.setattr(config, "BLUEPRINT_ENABLED", True)
    return projects.create("巡田無人機", vision="自動巡田")


def test_parse_blueprint_full(proj):
    data = blueprint.parse_blueprint(SAMPLE)
    assert data["vision"].startswith("讓小型農場")
    assert data["users"].startswith("自有")
    assert [f["priority"] for f in data["features"]] == [0, 0, 1, 1, 2]  # 無 tag → P1
    assert data["features"][0]["detail"].startswith("在地圖上")
    assert data["features"][1]["detail"] == ""  # 無說明可省
    assert len(data["milestones"]) == 2


def test_parse_blueprint_no_features_returns_none():
    assert blueprint.parse_blueprint("這段輸出完全沒有照格式來") is None
    assert blueprint.parse_blueprint("") is None


def test_save_load_roundtrip(proj):
    pid = proj["id"]
    assert not blueprint.exists(pid)
    data = blueprint.parse_blueprint(SAMPLE)
    blueprint.save(pid, data, session_id="s1")
    assert blueprint.exists(pid)
    loaded = blueprint.load(pid)
    assert loaded["session_id"] == "s1" and loaded["generated_at"] > 0
    assert len(loaded["features"]) == 5


def test_load_corrupt_json_returns_none(proj):
    pid = proj["id"]
    blueprint._json_path(pid).write_text("{broken", encoding="utf-8")
    assert blueprint.load(pid) is None


def test_render_and_write_md(proj):
    pid = proj["id"]
    data = blueprint.parse_blueprint(SAMPLE)
    md = blueprint.render_md(data, name="巡田無人機")
    assert md.startswith("# 產品藍圖：巡田無人機")
    assert "[P0]** 航線規劃" in md and "里程碑" in md
    blueprint.write_md(pid, md)
    assert (projects.workspace_dir(pid) / "BLUEPRINT.md").read_text(encoding="utf-8") == md


def test_seed_backlog_priority_and_cap(proj):
    pid = proj["id"]
    data = blueprint.parse_blueprint(SAMPLE)
    n = blueprint.seed_backlog(pid, data, cap=3)
    assert n == 3
    sdir = projects.state_dir(pid)
    first = backlog.next_pending(state_dir=sdir)
    assert first["priority"] == 0 and first["source"] == "blueprint" and first["type"] == "feature"
    # 已 seed 的標記住，重跑不重複；cap 內續餵剩餘功能。
    assert sum(1 for f in data["features"] if f.get("seeded")) == 3
    n2 = blueprint.seed_backlog(pid, data, cap=10)
    assert n2 == 2 and backlog.counts(state_dir=sdir)["pending"] == 5


def test_context_injection_and_gate(proj, monkeypatch):
    pid = proj["id"]
    data = blueprint.parse_blueprint(SAMPLE)
    blueprint.save(pid, data)
    ctx = blueprint.context(pid)
    assert "【產品藍圖" in ctx and "[P0] 航線規劃" in ctx and "M1" in ctx
    monkeypatch.setattr(config, "BLUEPRINT_ENABLED", False)
    assert blueprint.context(pid) == ""  # 開關關閉 → 空字串、零影響


def test_context_empty_without_blueprint(proj):
    assert blueprint.context(proj["id"]) == ""
