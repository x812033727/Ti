# `GH_PAT` Token 輪替工作單（2026-07-10）

狀態：待人工發新 token、更新 secret／`.env`、完成驗證後撤舊。本文不記錄、不貼上 token 明文。

## 固定順序

**發新 -> 更新 repo secret 與 `.env` -> 驗證 -> 撤舊**。新 token 未驗證通過前，不得撤銷舊 token。

| 步驟 | 負責 | 狀態 | 驗收 |
|------|------|------|------|
| 1. 發新 fine-grained PAT | 人工 | 待人工 | 只選本 repo；`Contents: Read and write`；secret 名稱固定 `GH_PAT`；設定到期日。 |
| 1/2. 更新 repo secret `GH_PAT` 與本機／部署 `.env` | 人工 | 待人工 | 明文只進 GitHub Actions secret 與本機／部署環境，不進對話、log、版控。 |
| 2. 驗證新 token | AI 可代勞 | 待執行 | `bash scripts/verify_token_rotation.sh --verify` 成功；`gh` 路徑必須等價 `GH_TOKEN="$GH_PAT" gh auth status`。 |
| 掃描. 殘留 token | AI 可代勞 | 待執行 | `bash scripts/verify_token_rotation.sh --scan [path ...]`；gitleaks 優先，grep fallback 需遮蔽命中。 |
| 3. 撤銷舊 token | 人工 | 待人工 | 只在步驟 2 通過後，到 GitHub UI 刪除舊 fine-grained PAT。 |

## AI 可執行指令

```bash
bash scripts/verify_token_rotation.sh --report
bash scripts/verify_token_rotation.sh --verify
bash scripts/verify_token_rotation.sh --scan .
```

如有 `history/` 目錄，另跑：

```bash
bash scripts/verify_token_rotation.sh --scan history
```

## Scope 核對

curl fallback 的 HTTP 200 只證明身分有效，不證 repository scope。人工撤舊前需核對四項規格：fine-grained token、只選本 repo、`Contents: Read and write`、secret 名稱 `GH_PAT`。

## 實作證據

- 驗證腳本：`scripts/verify_token_rotation.sh`
- 守門測試：`tests/docs/test_qa_token_rotation_script.py`
- Runbook：`docs/token-rotation-runbook.md`
