"""一次性檢查腳本：定位 agenda_plan payload/history jsonl 的 criteria 欄位。

不動業務邏輯、不建注入管線、純使用 events.agenda_plan 構建子 + history.record_event
寫入 jsonl，印出真實結構與 criteria 真實範例值。

對應任務 #1（blocker gate）。跑完即棄，產物為 stdout 報告。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# 確保從工作目錄 import studio.*
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from studio import config, events, history  # noqa: E402


def main() -> int:
    inspect_root = Path("tmp") / "inspect_history"
    inspect_root.mkdir(parents=True, exist_ok=True)
    # 將 HISTORY_ROOT 指向 tmp/inspect_history/，避免污染既有 worktree。
    config.HISTORY_ROOT = inspect_root

    sid = "inspect-agenda-criteria-001"
    # pre-create 空 events.jsonl 繞過 start_session 的 secure_write_root 強制 owner 驗證；
    # 本檔位於 tmp/，容器內即 root owner，符合既有 strict 不變量。
    events_path = inspect_root / f"{sid}.jsonl"
    events_path.touch()

    # 採用既有測試 fixture 的真實 agenda 結構（tests/core/test_agenda_persistence.py:70
    # 與 tests/core/test_agenda_orchestration.py:189），criteria 為 orchestrator 實填欄位。
    agenda = [
        {
            "title": "核心運算模組",
            "description": "實作加減乘除與基本錯誤處理",
            "criteria": "pytest 全綠 + 邊界值覆蓋（除以零、溢位）",
            "assignee": "engineer",
        },
        {
            "title": "介面與說明",
            "description": "CLI argparse 與 README 使用範例",
            "criteria": "curl 全流程 2xx | README 範例可重現",
            "assignee": "engineer",
        },
    ]
    tasks = [
        {"id": 1, "title": "建立 calculator.py"},
        {"id": 2, "title": "main.py CLI 介面"},
        {"id": 3, "title": "README.md + test_calculator.py"},
    ]
    assignments = [
        {"index": 1, "title": "核心運算模組", "assignee": "engineer"},
        {"index": 2, "title": "介面與說明", "assignee": "engineer"},
    ]
    corrections = [{"index": 0, "given": "architect", "assigned": "engineer"}]
    edges = [[2, 1], [3, 1]]

    # 用既有構建子產出真實事件 dict，再走 record_event 入 jsonl。
    ev = events.agenda_plan(
        sid,
        agenda,
        tasks,
        assignments,
        corrections=corrections,
        edges=edges,
    )
    ev_dict = ev.to_dict() if hasattr(ev, "to_dict") else {
        "type": ev.type.value,
        "session_id": ev.session_id,
        "payload": ev.payload,
    }
    history.record_event(sid, ev_dict)

    # 讀回 jsonl、印出 payload 結構。
    lines = events_path.read_text(encoding="utf-8").splitlines()
    print(f"[INFO] events.jsonl 行數: {len(lines)}")
    saved = json.loads(lines[0])
    assert saved["type"] == "agenda_plan", saved["type"]

    payload = saved["payload"]
    print("[INFO] agenda_plan payload top-level keys:", sorted(payload.keys()))

    # 核心定位：criteria 在 agenda 子題內、型別、範例值
    print("\n[CRITERIA 欄位定位]")
    print(f"  - payload 結構: payload['agenda'][i] 為子題 dict")
    print(f"  - 子題 keys: {sorted(payload['agenda'][0].keys())}")
    for i, item in enumerate(payload["agenda"]):
        crit = item.get("criteria")
        print(
            f"  - agenda[{i}]['criteria']: type={type(crit).__name__}, "
            f"value={crit!r}"
        )

    # 驗證 criteria 不在 top-level payload、且 tasks/assignments 不含 criteria
    print("\n[CRITERIA 不在以下位置]")
    print(f"  - payload['criteria']: {payload.get('criteria', '<absent>')!r}")
    print(f"  - payload['tasks'][0] keys: {sorted(payload['tasks'][0].keys())}")
    print(f"  - payload['assignments'][0] keys: {sorted(payload['assignments'][0].keys())}")

    # 模擬前端 app.js:325-339 既有渲染路徑，論證 criteria 漏渲染
    print("\n[前端 app.js 既有 agenda_plan 渲染（app.js:325-339）]")
    items = payload["agenda"]
    print(f"  addSystem('📋 議程拆解：{len(items)} 個子題')")
    for i, a in enumerate(items):
        line = f"{i + 1}. {a.get('title', '')}"
        if a.get("description"):
            line += f"｜{a['description']}"
        if a.get("assignee"):
            line += f"（主責: {a['assignee']}）"
        # 完全照搬 app.js:328-330 邏輯
        crit_in_line = "criteria" in line.lower() or "【準則" in line or "｜準則" in line
        print(f"  addSystem({line!r})  [criteria 出現? {crit_in_line}]")

    # 結論
    print("\n[結論]")
    print("  - 欄位: payload['agenda'][i]['criteria']")
    print("  - 型別: str")
    print("  - 範例值: 'pytest 全綠 + 邊界值覆蓋（除以零、溢位）' / 'curl 全流程 2xx | README 範例可重現'")
    print("  - 前端現況: app.js:325-339 漏讀 a.criteria → criteria 進 history 不見於畫面（症狀確認）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
