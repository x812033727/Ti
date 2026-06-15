# 驗收執行指令統一入口（架構決策）。
#
# 為何用 Makefile 而非裸 `python ...`：本環境只有 `python3`，無 `python`；
# 直接 `python -m pytest` 會 command not found 被誤判為測試紅。Makefile 跟著
# repo 走、環境無關，比機器層 shim（換 CI 又要重設）可逆性更高。
PYTHON ?= python3

.PHONY: test test-release

# 任務 #2 / 閉環 release 流程驗收：tag notes 取值介面 + workflow 守護測試。
test-release:
	$(PYTHON) -m pytest tests/autopilot/ -q -k "release"

# 全測試集。
test:
	$(PYTHON) -m pytest -q
