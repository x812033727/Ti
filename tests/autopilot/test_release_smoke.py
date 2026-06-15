"""QA 任務 #3：驗證 release smoke 檢查邏輯（`studio.release_smoke`）。

對應驗收標準：
  #2 body 含非空頂層 Breaking Changes 區塊 → 通過；缺或空 → 非零退出。
  #3 判定邏輯 import SSOT（`extract_breaking_block` / `BREAKING_HEADING`）；
     smoke 模組無重複 heading 字面值。
  #4 反向黑樣本：抽掉 body 的 Breaking Changes 區塊時測試必翻紅，且含 mutation
     非空斷言（證明確實改到目標、非永遠綠）。
  #5 smoke 失敗時 stderr 印 body 前 500 字片段。

破壞性原則：預設東西是壞的——重點壓在邊界（空字串、缺區塊、空區塊、fence 內偽
heading、超長 body 截斷、env vs stdin 兩條路徑、空 body 的 `<empty>` 標記）與
錯誤路徑，不只測快樂路徑。每個「通過」斷言旁都放一條對照黑樣本，杜絕假綠。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from studio.release_note import BREAKING_HEADING, extract_breaking_block
from studio.release_smoke import check_body, main

ROOT = Path(__file__).resolve().parents[2]

# --- 樣本 body：用 SSOT 常數組裝，不在測試硬寫 heading 字面值（除 heading_contract 例外）。 ---

_NONEMPTY_BLOCK_BODY = f"""# Release 0.2.0

{BREAKING_HEADING}

- ① 行為變動：移除舊 API
- ② 原因：安全性
- ③ before / after：見遷移指南
- ④ 生效版本：0.2.0

## 其他變更

- 雜項
"""

# 缺整個 Breaking Changes 區塊（heading 都沒有）。
_MISSING_BLOCK_BODY = """# Release 0.2.0

## 其他變更

- 雜項修補
"""

# heading 存在但區塊內容為空（下一個頂層 section 緊接，中間只有空白）。
_EMPTY_BLOCK_BODY = f"""# Release 0.2.0

{BREAKING_HEADING}

## 其他變更

- 雜項
"""


# ===========================================================================
# 正樣本：含非空頂層 Breaking Changes 區塊 → check_body 不 raise（AC#2）
# ===========================================================================

def test_nonempty_block_passes():
    """快樂路徑：含非空區塊時 check_body 正常返回 None。"""
    assert check_body(_NONEMPTY_BLOCK_BODY) is None


def test_mutation_guard_positive_sample_block_is_actually_nonempty():
    """mutation 非空防護：證明正樣本的區塊『確實非空』，否則『通過』只是假綠。

    若哪天 extractor 改壞、對正樣本回 None 或空字串，這條會先翻紅，揭穿
    test_nonempty_block_passes 其實沒測到東西。
    """
    block = extract_breaking_block(_NONEMPTY_BLOCK_BODY)
    assert block is not None, "正樣本應抽得出區塊；抽不到代表測試喪失鑑別力"
    assert block.strip(), "正樣本區塊必須非空，否則『通過』是假綠"
    assert "① 行為變動" in block, "抽出的應是 Breaking Changes 區塊本身，非別處內容"


# ===========================================================================
# 反向黑樣本：缺 / 空區塊 → raise ValueError（AC#2、#4 真鑑別力）
# ===========================================================================

def test_missing_block_raises():
    """缺整個 Breaking Changes 區塊 → 必 raise。"""
    with pytest.raises(ValueError):
        check_body(_MISSING_BLOCK_BODY)


def test_empty_block_raises():
    """heading 在但區塊內容為空 → 必 raise（不可因有 heading 就放行）。"""
    with pytest.raises(ValueError):
        check_body(_EMPTY_BLOCK_BODY)


def test_empty_string_body_raises():
    """整個 body 為空字串 → 必 raise。"""
    with pytest.raises(ValueError):
        check_body("")


def test_mutation_removes_block_flips_red():
    """mutation 鑑別力：從『通過的正樣本』抽掉區塊，必須從 pass 翻成 raise。

    這是 AC#4 的核心：證明黑樣本與正樣本的唯一差異就是那個區塊，且抽掉它
    真的會翻紅——而非無論改不改都綠。
    """
    # 前提：原樣本確實會通過（非永遠綠）。
    assert check_body(_NONEMPTY_BLOCK_BODY) is None

    # mutation：移除 heading 行與其下內容，模擬「區塊被抽掉」。
    mutated = _NONEMPTY_BLOCK_BODY.replace(BREAKING_HEADING, "## 一般變更")
    assert BREAKING_HEADING not in mutated, "mutation 必須真的改掉目標 heading"

    with pytest.raises(ValueError):
        check_body(mutated)


# ===========================================================================
# fence 感知：區塊內 code fence 裡的偽 `## ` 不應被當邊界截斷成假綠
# ===========================================================================

def test_fenced_pseudo_heading_inside_block_still_nonempty():
    """區塊內 fence 裡含 `## ` 註解時，區塊不被截斷、仍判為非空 → 通過。"""
    body = f"""# Release 0.2.0

{BREAKING_HEADING}

範例：

```sh
## 這是 shell 註解，不是 section 邊界
echo hi
```

實際變更說明。
"""
    # 對照：必須通過（fence 內偽 heading 不該誤截成空）。
    assert check_body(body) is None
    block = extract_breaking_block(body)
    assert "echo hi" in block, "fence 內容應保留在區塊內，未被偽 heading 截斷"


# ===========================================================================
# CLI / main()：env BODY 與 stdin 兩路徑、exit code、stderr body 片段（AC#5）
# ===========================================================================

def test_main_env_body_pass(monkeypatch):
    """main 讀 env BODY，含非空區塊 → 回 0。"""
    monkeypatch.setenv("BODY", _NONEMPTY_BLOCK_BODY)
    assert main() == 0


def test_main_env_body_fail(monkeypatch, capsys):
    """main 讀 env BODY，缺區塊 → 回 1，且 stderr 印 body 片段。"""
    monkeypatch.setenv("BODY", _MISSING_BLOCK_BODY)
    rc = main()
    assert rc == 1
    err = capsys.readouterr().err
    assert "release smoke 失敗" in err
    # AC#5：失敗時印 body 片段供人工審查，而非僅 "not found"。
    assert "雜項修補" in err, "stderr 應含實際 body 片段"


def test_main_stdin_fallback(monkeypatch):
    """未設 env BODY 時，fallback 讀 stdin。"""
    monkeypatch.delenv("BODY", raising=False)

    class _FakeStdin:
        @staticmethod
        def read():
            return _NONEMPTY_BLOCK_BODY

    monkeypatch.setattr(sys, "stdin", _FakeStdin)
    assert main() == 0


def test_main_body_preview_truncated_to_500(monkeypatch, capsys):
    """AC#5：失敗時 body 片段截斷至前 500 字（不灌爆 log）。"""
    long_body = "X" * 5000  # 無區塊 → 必 fail
    monkeypatch.setenv("BODY", long_body)
    assert main() == 1
    err = capsys.readouterr().err
    # 印出的 X 數量恰為 500（前綴），不應是全部 5000。
    assert err.count("X") == 500, "body 片段應截斷至前 500 字"


def test_main_empty_body_prints_empty_marker(monkeypatch, capsys):
    """空 body 失敗時印明確 `<empty>` 標記，避免『看起來沒執行』的誤判。"""
    monkeypatch.setenv("BODY", "")
    assert main() == 1
    err = capsys.readouterr().err
    assert "<empty>" in err


# ===========================================================================
# SSOT 重用 / 無重複 heading 字面值（AC#3）— 對 smoke 模組原始碼做靜態斷言
# ===========================================================================

def test_smoke_module_imports_ssot():
    """smoke 模組必須 import SSOT extractor，而非自寫判定。"""
    src = (ROOT / "studio" / "release_smoke.py").read_text(encoding="utf-8")
    assert "from studio.release_note import" in src
    assert "extract_breaking_block" in src


def test_smoke_module_has_no_heading_literal():
    """AC#3：smoke 模組不得出現 heading 字面值（含 emoji 版），一律 import 常數。"""
    src = (ROOT / "studio" / "release_smoke.py").read_text(encoding="utf-8")
    assert BREAKING_HEADING not in src, "smoke 模組不得硬寫 heading 字面值"
    # 連去 emoji 的弱化字面值也不該出現（防漂移）。弱化版由常數衍生，
    # 本測試檔遂達成零 heading 字面值，AC#4 grep 範圍可安全涵蓋本檔。
    weakened_heading = BREAKING_HEADING.replace("⚠️ ", "")
    assert weakened_heading not in src


# ===========================================================================
# 端到端：以子行程實跑 `python -m studio.release_smoke`，驗 CLI 真實退出碼
# ===========================================================================

def test_cli_subprocess_pass_and_fail():
    """端到端：實際以子行程跑 CLI，確認 env BODY 路徑的退出碼正確。

    不只測 main() 函式，連『真的當程式跑』都驗一次，避免 __main__ 黏合層出錯。
    """
    base_env = {"PYTHONPATH": str(ROOT)}

    ok = subprocess.run(
        [sys.executable, "-m", "studio.release_smoke"],
        env={**base_env, "BODY": _NONEMPTY_BLOCK_BODY},
        capture_output=True, text=True, cwd=str(ROOT),
    )
    assert ok.returncode == 0, f"正樣本應退出 0，stderr={ok.stderr}"

    bad = subprocess.run(
        [sys.executable, "-m", "studio.release_smoke"],
        env={**base_env, "BODY": _MISSING_BLOCK_BODY},
        capture_output=True, text=True, cwd=str(ROOT),
    )
    assert bad.returncode == 1, "黑樣本應非零退出"
    assert "雜項修補" in bad.stderr, "CLI 失敗應印 body 片段"
