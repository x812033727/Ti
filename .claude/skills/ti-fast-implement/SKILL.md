---
name: ti-fast-implement
description: 原生快車道實作程序。何時用:被指派「獨立完成任務」的快車道工程場——開工前讀一次,收尾前照自查清單走。
---

# 原生快車道實作程序

你是快車道的唯一工程師:沒有評審員、沒有 PM 跟場——你收工後直接進系統客觀閘門
(lint/collect/測試/合併)。閘門紅=任務重排燒 attempts,所以**自查就是你的評審**。

## 開工判斷(先想 30 秒再動手)

符合任一條就**不要動手**,只輸出一行 `需完整管線: <一句原因>` 後結束:

- 跨子系統的架構改動(動 orchestrator/runner/發佈流程等核心契約)
- 高風險遷移(資料格式/狀態檔 schema/外部 API 契約變更)
- 需求本身需要多視角評審或含糊到需要討論才能定案

小而明確的 bug 修復、單一子系統的功能補強、文件/測試補齊=直接做。

## 實作紀律

- 小步實作,改哪個子系統就先跑該子系統測試(tests/autopilot、tests/core、tests/server、tests/docs)。
- **不要 git push、不要開 PR、不要 commit**——系統會接手閘門與合併。
- 新 TI_ 旋鈕:config.py 兩處(頂層+reload 區塊+reload 的 global 行)+.env.example,缺一 CI 紅。
- 新 API 端點要登記 docs/loopback-endpoint-audit.md(寫端點另有 audit 測試 expected set)。

## 收尾自查(必跑,全綠才算收工)

```bash
timeout 120 .venv/bin/python -m ruff check . --fix
timeout 120 .venv/bin/python -m ruff format .
timeout 300 .venv/bin/python -m pytest -q  # 至少改動子系統;時間許可跑全套
```

殘紅就修到綠;修不動的紅=誠實說明卡在哪,不要假裝完成。
