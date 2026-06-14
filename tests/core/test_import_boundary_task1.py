"""任務 #1：釘住 providers→experts 的 import 路徑與 SDK 依賴邊界。

目標——確認「providers 共用 experts.make_retry_config」這條 import 路徑，在 CI 無
LLM SDK（claude-agent-sdk／openai／anthropic 皆未安裝）的環境下不會觸發 import 失敗：

1. experts.py／providers.py 頂層皆不得 import 任何 LLM SDK（SDK 一律 lazy，於建構或
   呼叫時才 local import）——AST 靜態確認，把錯誤攔在最便宜的階段。
2. 在「主動屏蔽 SDK」的乾淨子程序中（不論本機是否安裝 SDK，皆模擬 CI 無 SDK）：
   - `import studio.experts` / `import studio.providers` 退出碼 0、不拉 SDK 進 sys.modules；
   - `from studio.experts import make_retry_config` 可取得並回傳 RetryConfig，三參數齊備。
3. 反向黑樣本（排假綠）：同一屏蔽器下，直接 `import claude_agent_sdk` 必須 ModuleNotFoundError
   ——證明屏蔽真的生效，上面的「import 成功」才有判別力，而非 SDK 其實裝著的假綠。

此測試是 #2（providers.speak 接 run_with_retries）的回歸護欄：日後若有人在 experts.py
頂層誤引入 SDK import，本測試會在無 SDK 的 CI 立即轉紅。
"""

from __future__ import annotations

import ast
import subprocess
import sys

from _repo import REPO_ROOT

from studio import experts, providers

# 任何根模組命中即視為「LLM SDK」——禁止在 providers/experts 頂層出現。
_SDK_ROOTS = {"claude_agent_sdk", "openai", "anthropic"}

# 在子程序最前面安裝一個 meta-path finder，主動屏蔽 SDK：無論本機是否安裝，
# 後續 import 一律得到 ModuleNotFoundError，等價於 CI 乾淨無 SDK 的環境。
_BLOCK_SDK = (
    "import sys\n"
    "_BLOCKED = {'claude_agent_sdk', 'openai', 'anthropic'}\n"
    "class _Blocker:\n"
    "    def find_spec(self, name, path=None, target=None):\n"
    "        if name.split('.')[0] in _BLOCKED:\n"
    "            raise ModuleNotFoundError('blocked SDK: ' + name)\n"
    "        return None\n"
    "sys.meta_path.insert(0, _Blocker())\n"
)


def _run_no_sdk(code: str) -> subprocess.CompletedProcess:
    """在屏蔽 SDK 的乾淨子程序跑 code；cwd 釘 REPO_ROOT 使 `import studio` 可解析。"""
    return subprocess.run(
        [sys.executable, "-c", _BLOCK_SDK + code],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=str(REPO_ROOT),
    )


def _top_level_imports(module) -> list[str]:
    """抽出模組『頂層』(module body) 的 import 名稱；函式/方法內與 TYPE_CHECKING 區塊
    的 local import 不算（那才是 SDK 該待的地方）。"""
    tree = ast.parse(open(module.__file__, encoding="utf-8").read())
    names: list[str] = []
    for node in tree.body:  # 僅遍歷頂層節點，不深入函式體
        if isinstance(node, ast.Import):
            names += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            names.append(node.module or "")
    return names


# --- 1) 頂層 import 靜態邊界（最便宜的攔截點） ---


def test_experts_top_level_has_no_sdk_import():
    names = _top_level_imports(experts)
    assert not any(n.split(".")[0] in _SDK_ROOTS for n in names), names


def test_providers_top_level_has_no_sdk_import():
    names = _top_level_imports(providers)
    assert not any(n.split(".")[0] in _SDK_ROOTS for n in names), names


# --- 2) 無 SDK 子程序：import 路徑可用 ---


def test_import_experts_without_sdk():
    r = _run_no_sdk(
        "import studio.experts; "
        "import sys; "
        "assert 'claude_agent_sdk' not in sys.modules, 'experts 不該載入時就拉 SDK'; "
        "print('OK')"
    )
    assert r.returncode == 0, r.stderr
    assert "OK" in r.stdout


def test_import_providers_without_sdk():
    r = _run_no_sdk(
        "import studio.providers; "
        "import sys; "
        "assert 'claude_agent_sdk' not in sys.modules; "
        "assert 'openai' not in sys.modules; "
        "print('OK')"
    )
    assert r.returncode == 0, r.stderr
    assert "OK" in r.stdout


def test_make_retry_config_importable_and_usable_without_sdk():
    """核心驗收：無 SDK 下 from studio.experts import make_retry_config 可用，
    回傳 RetryConfig 且退避三參數（max_retries/backoff/sleep）齊備。"""
    r = _run_no_sdk(
        "from studio.experts import make_retry_config; "
        "from studio.llm_caller import RetryConfig; "
        "cfg = make_retry_config(); "
        "assert isinstance(cfg, RetryConfig), type(cfg); "
        "kw = cfg.as_kwargs(); "
        "assert set(kw) == {'max_retries', 'backoff', 'sleep'}, kw; "
        "print('OK')"
    )
    assert r.returncode == 0, r.stderr
    assert "OK" in r.stdout


def test_providers_can_import_make_retry_config_from_experts_without_sdk():
    """釘住 #2 將採用的具體 import 形式：`from studio.experts import make_retry_config`
    在 providers 已載入後仍可用（共用同一工廠、同一 EXPERT_RATE_LIMIT_* 旋鈕）。"""
    r = _run_no_sdk(
        "import studio.providers; "
        "from studio.experts import make_retry_config; "
        "assert callable(make_retry_config); "
        "assert make_retry_config().max_retries >= 0; "
        "print('OK')"
    )
    assert r.returncode == 0, r.stderr
    assert "OK" in r.stdout


# --- 3) 反向黑樣本：證明屏蔽真的生效（排假綠） ---


def test_sdk_truly_blocked_in_subprocess():
    """若屏蔽失效（SDK 其實可 import），上面所有『無 SDK 仍成功』都是假綠。
    此處明文證明：同一屏蔽器下，直接 import SDK 必拋 ModuleNotFoundError。"""
    r = _run_no_sdk(
        "import sys\n"
        "for name in ('claude_agent_sdk', 'openai', 'anthropic'):\n"
        "    try:\n"
        "        __import__(name)\n"
        "    except ModuleNotFoundError:\n"
        "        continue\n"
        "    else:\n"
        "        raise AssertionError('屏蔽失效，' + name + ' 仍可 import')\n"
        "print('OK')"
    )
    assert r.returncode == 0, r.stderr
    assert "OK" in r.stdout
