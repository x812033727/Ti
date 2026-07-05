"""_discover 進場過濾的 done 相似度去重（任務 #2）：done-list 從精確比對升級為相似度比對。

釘住行為變更（高工退回要求）：
1. 與近期 done 標題「改寫過但詞集 Jaccard ≥ AUTOPILOT_DEDUP_RATIO」的提案會被擋掉（證明相似度升級生效，非精確）。
2. AUTOPILOT_EVAL_MEMORY=0 時同一筆被保留（證明開關回退＝舊關閉行為，向後相容）。

走離線假專家路徑（OFFLINE_MODE=True → items 直接取自 OFFLINE_DISCOVERY），不打外部 API。
黑白樣本依 PM 定案（選 C）：黑＝同核心詞、僅語序/修飾改寫；白＝語意無關。
"""

from __future__ import annotations

import pytest

from studio import backlog, config, projects
from studio.improver import ProjectImprover

# 對照已 done 的「改善去重邏輯」：
#   黑（同核心詞、僅語序改寫，sim≥0.75 應被擋）＝「去重邏輯改善」
#   白（語意無關，應放行）＝「新增登入頁面深色模式」
_DONE_TITLE = "改善去重邏輯"
_BLACK = "去重邏輯改善"
_WHITE = "新增登入頁面深色模式"


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PROJECTS_ROOT", tmp_path / "projects")
    monkeypatch.setattr(config, "WORKSPACE_ROOT", tmp_path / "ws")
    monkeypatch.setattr(config, "OFFLINE_MODE", True)  # items 直接取自 OFFLINE_DISCOVERY
    # 離線提案＝一黑一白，聚焦 done 過濾段
    monkeypatch.setattr("studio.improver.OFFLINE_DISCOVERY", [_BLACK, _WHITE])


def _improver_with_done():
    """建 project，並在其 backlog 種一筆 done 標題作為 done-list corpus。"""
    project = projects.create("去重測試產品", vision="v")
    sdir = projects.state_dir(project["id"])
    task = backlog.add(_DONE_TITLE, source="seed", state_dir=sdir)
    backlog.set_status(task["id"], "done", state_dir=sdir)

    async def bc(ev):
        pass

    return ProjectImprover(project, bc), sdir


@pytest.mark.asyncio
async def test_rewritten_done_title_blocked_by_similarity(monkeypatch):
    """EVAL_MEMORY>0：改寫版重提（同核心詞）被 done 相似層擋下，只入列語意無關的白樣本。"""
    monkeypatch.setattr(config, "AUTOPILOT_EVAL_MEMORY", 5)
    imp, sdir = _improver_with_done()

    n = await imp._discover(sdir)

    pending = [t["title"] for t in backlog.list_tasks("pending", state_dir=sdir)]
    assert _BLACK not in pending  # 改寫版被相似度擋下（精確比對擋不到）
    assert _WHITE in pending  # 語意無關正常放行、不誤殺
    assert n == 1


@pytest.mark.asyncio
async def test_eval_memory_zero_restores_exact_behavior(monkeypatch):
    """EVAL_MEMORY=0：done corpus 為空 → 相似層全放行，改寫版被保留（退回舊關閉行為）。"""
    monkeypatch.setattr(config, "AUTOPILOT_EVAL_MEMORY", 0)
    imp, sdir = _improver_with_done()

    n = await imp._discover(sdir)

    pending = [t["title"] for t in backlog.list_tasks("pending", state_dir=sdir)]
    assert _BLACK in pending  # 開關關閉：改寫版不再被擋，向後相容
    assert _WHITE in pending
    assert n == 2
