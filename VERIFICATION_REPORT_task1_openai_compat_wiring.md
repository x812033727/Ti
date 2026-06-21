# 任務 #1 驗證報告：OpenAI 相容 provider 接線

## 結論

openai / minimax / gemini 三個 OpenAI 相容 provider 的 production 接線已成立，無需修改 `studio/`。

- `OpenAIExpert.speak()` 會在進入工具迴圈前取 `make_retry_config()`，並把整輪 `_attempt` 交給 `llm_caller.run_with_retries(...)`：`studio/providers.py:825`, `studio/providers.py:841`, `studio/providers.py:849`, `studio/providers.py:991`
- `make_expert()` 對 `("openai", "minimax", "gemini")` 統一建立 `OpenAIExpert`，差異只在 `_chat_for(prov)` 與 `openai_model_for(role)`：`studio/providers.py:1065`, `studio/providers.py:1072`, `studio/providers.py:1077`
- `_chat_for(provider)` 最終都呼叫 `_openai_chat(..., provider=provider)`：`studio/providers.py:1053`, `studio/providers.py:1059`
- `_openai_chat()` lazy import `openai` 後建立 `AsyncOpenAI(..., max_retries=0)`，SDK 內建 retry 已讓位給外層 `run_with_retries`：`studio/providers.py:1039`, `studio/providers.py:1043`, `studio/providers.py:1046`
- `_openai_client_args()` 僅依 provider 分流 key/base_url；minimax/gemini 沒有繞過 `_openai_chat`：`studio/providers.py:1024`, `studio/providers.py:1031`, `studio/providers.py:1034`

## 測試佐證

- `tests/core/test_providers.py:192` 驗證 minimax 建成 `OpenAIExpert`
- `tests/core/test_providers.py:199` 驗證 gemini 建成 `OpenAIExpert` 且 `_provider == "gemini"`
- `tests/core/test_wiring_retry_config.py:104` 驗證 `speak()` 實收 `run_with_retries` 三參數，且不是 import-time 快照
- `tests/core/test_providers_max_retries_task1_qa.py:68` 驗證 openai 的 `AsyncOpenAI` 收到 `max_retries=0`
- `tests/core/test_providers_max_retries_task1_qa.py:85` 驗證 minimax 共用 `_openai_chat` 並收到 `max_retries=0`
- `tests/core/test_providers_max_retries_task1_qa.py:118` 驗證 patch 接縫是 `sys.modules["openai"]`

gemini 的專門 `max_retries=0` runtime 測試仍屬任務 #2；任務 #1 的 code-path 驗證顯示 production 接線沒有漏接。

## 自測

- `python3 -m pytest tests/core/test_providers.py::test_make_expert_minimax tests/core/test_providers.py::test_make_expert_gemini tests/core/test_providers.py::test_openai_client_args_minimax tests/core/test_providers.py::test_openai_client_args_gemini tests/core/test_providers_max_retries_task1_qa.py tests/core/test_retry_convergence_task5_qa.py -q` -> `17 passed`
- `timeout 60 python3 -m pytest tests/core/ -q` -> `765 passed, 1 warning`

## 異動

- production code：無
- 測試碼：無
- 文件：新增本報告
