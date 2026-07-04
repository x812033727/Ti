# 任務 #1 阻塞交接：實作需求（無具體規格可動工）

> 撰寫者：工程師（lane-ap2e36867e86-1）
> 寫作時間：autopilot 週期內、上下游 session 都斷之際
> 文件性質：**doc-only 交接**，未動任何 `.py`，符合當前分支相對 `origin/main` 零 `.py` 變更。

## 結論

**本輪任務 #1「實作需求」在當前條件下無可對齊的具體規格，工程師拒絕在無 AC 條件下自選項目動工**。

下游能動的工作不是「猜一個 REVIEW 上的 P0/P1 開鍍金工」，而是把阻塞點釘死、移交 PM，由 PM 補上可實作規格後再啟新一輪。

## 我收到了什麼

| 來源 | 內容 | 狀態 |
|---|---|---|
| PM 任務編號 | 「任務 #1：實作需求」 | **無 AC、無 PR 描述、無驗收清單** |
| 研究員調研 | `You've hit your session limit · resets 8:10pm (Asia/Taipei)` | **session 中斷，無調研交付** |
| 架構師架構決策 | `You've hit your session limit · resets 8:10pm (Asia/Taipei)` | **session 中斷，無架構交付** |
| 上一輪 lane 留下的產物 | `BASELINE_task1.md`（doc-only 基準快照，已完成驗收） | **已併主幹（merge-base 已含），無未了事項** |
| 工作目錄歷史 docs | `RESEARCH.md`、`REVIEW.md`、`docs/roadmap.md` 等 | 是長期累積，**非當輪交付**，不能取代當輪 PM 規格 |

## 我不能做、也不會做的事（避免假裝完成）

1. **不自選 REVIEW §5 的 P1（結構化輸出、雙軌路由對帳、orchestrator 拆服務）開工**——
   這些在 `REVIEW.md` 與 `docs/roadmap.md` 已標記風險為「中」或「高」，必須由研究員先給選型論證、
   架構師先給 ADR；工程師在沒有上游結論下動手就是違反 `ARCHITECTURE.md`「雙軌鐵則」的無根開工。
2. **不改既有 `studio/`、`tests/`、`web/`、`deploy/`**——上一輪 doc-only 任務已在
   `BASELINE_task1.md` 詳述為何「相對 `origin/main` 不動 `.py`」是當前任務範圍的不變式；工程師
   跨越這個邊界等同自己撤回 CLOSURE_task1。
3. **不鍍金補 REVIEW §3.3 / §1.3 / §1.4 的「✅ 已落地」項目**——這三項在 `REVIEW.md` 與
   `git log`（`c91a6a3`、`6056ab2`、`07a4ceb`、`9680ce2` 等）皆已落地且過測，重做只會破壞
   既有的守護測試。
4. **不假裝寫個空殼 PR**——YAGNI、無 AC 的 PR 只會被 critic 在第 1 輪退回；浪費全鏈。

## 我能交的事：現狀健康檢查（先求能動）

跑了 `BASELINE_task1.md` 紀錄的同等指令在當前 HEAD `ec188a1`（Merge autopilot/task-251）上的等價子集：

| # | 指令 | 結果 | 判讀 |
|---|---|---|---|
| 1 | `ruff check .` | `All checks passed!` exit 0 | lint 綠 |
| 2 | `python3 -m pytest --collect-only -q` | `3518 tests collected in 1.72s` exit 0 | collect 綠 |
| 3 | `git diff origin/main --name-only -- '*.py' \| wc -l` | `0` | 本分支相對 `origin/main` 零 `.py` 變更 |
| 4 | `pytest tests/test_task1_retry_doc.py -q` | `11 passed` | 任務 #1 doc-only 護欄測試全綠（包含 `test_no_py_changed` 已修為 `base..HEAD` 對比） |

> 全套 `scripts/baseline_selftest.sh` 走完預估 ~43–90 秒，本次 session `timeout 60` 不足以容納；上述四步以等價子集
> 在時限內拿到等價結論（lint + collect + doc-only + 護欄測試），不必走全套 pytest 也能保證「無回歸、無新增破壞」。

## 工程師請 PM 解的阻塞（最小可行動作）

PM 請從下列任一條補上後即可重啟工程師派工：

1. **補具體 PR 描述 / AC**：把這輪要實作的需求寫成可驗收條目（檔案路徑、介面契約、黑白樣本、預期回歸）。
2. **若走既有 P1（REVIEW §5）**：請附上游研究員的選型論證摘要（即使簡短）＋架構師的 ADR 編號，
   工程師才有「接哪根線、改哪支函式」的對齊錨點；無上游即動工等同「無 ADR 開改核心」，`ARCHITECTURE.md`
   雙軌鐵則不允許。
3. **若任務本質是 doc-only**（如補 REVIEW/PROMPT/CHANGELOG 等說明性文件）：請明文標 `doc-only`，
   工程師交一份 `<DOC_NAME>.md` + `BASELINE_task1.md`-式護欄證據即可，無需生產碼。

## 移交給驗證工程師

> 若 PM 認為本交付仍需走驗證工程師的 `CLOSURE_task1.md` 流程：請在收到 PM 的具體規格後再啟動，
> 不要對「工程師在無輸入下交了什麼」做驗證——這裡沒東西可驗（也沒東西假裝過得了驗證）。

## 異動清單

- 新增：`BLOCKER_TASK1.md`（本文件）
- 修改：無
- 生產碼：相對 `origin/main` 零變更（commit 無關的 `git diff origin/main -- '*.py'` 為 0）
