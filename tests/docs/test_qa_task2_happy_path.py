"""任務 #2 驗收：「執行環境前置」段新增最短「首次設定 happy-path」。

對應 PM 驗收標準 2、3、6：
- 一條從零到啟動的連續步驟流：clone → venv → pip install -e ".[dev]"
  → cp .env.example .env → （選填）pre-commit install → 啟動 → 開 localhost:8000。
- 順序正確、指令可複製貼上（同一個 code block）。
- pre-commit install 列入流程並標『選填』。
- 套件指令一律 .venv/bin/python3 -m pip（流程內禁裸 pip install）。
"""

import re

from _repo import REPO_ROOT

ROOT = REPO_ROOT
README = (ROOT / "README.md").read_text(encoding="utf-8")


def _section(title: str) -> str:
    m = re.search(
        rf"^##\s+{re.escape(title)}\s*$(.*?)(?=^##\s|\Z)", README, re.MULTILINE | re.DOTALL
    )
    assert m, f"找不到段落: ## {title}"
    return m.group(1)


SEC = _section("執行環境前置")


def _happy_block() -> str:
    """擷取 happy-path 子標題後的第一個 ```bash code block。"""
    m = re.search(r"happy-path.*?```bash\n(.*?)```", SEC, re.DOTALL)
    assert m, "『執行環境前置』段找不到 happy-path 的 bash code block"
    return m.group(1)


# ---- happy-path 子標題存在於前置段內 ----
def test_happy_path_heading_present():
    assert re.search(r"###\s+首次設定\s*happy-path", SEC), "缺『首次設定 happy-path』子標題"


# ---- 各步驟皆在同一 code block（可整段複製貼上） ----
def test_block_has_all_steps():
    b = _happy_block()
    assert "git clone" in b, "缺 clone"
    assert "python3 -m venv .venv" in b, "缺 venv"
    assert re.search(r'-m pip install -e "\.\[dev\]"', b), "缺 pip install -e .[dev]"
    assert "cp .env.example .env" in b, "缺 cp .env.example .env"
    assert re.search(r"pre[_-]commit install", b), "缺 pre-commit install"
    assert "-m studio.server" in b, "缺 啟動 studio.server"


# ---- localhost:8000 在 happy-path 區塊（標題之後的前置段內） ----
def test_localhost_8000_present():
    seg = SEC[SEC.find("happy-path") :]
    assert "http://localhost:8000" in seg, "happy-path 區塊缺 http://localhost:8000"


# ---- 順序正確：clone < venv < pip install < cp .env < pre-commit < server ----
def test_steps_in_correct_order():
    b = _happy_block()
    order = [
        ("clone", b.find("git clone")),
        ("venv", b.find("python3 -m venv .venv")),
        ("pip install", b.find('pip install -e ".[dev]"')),
        ("cp .env", b.find("cp .env.example .env")),
        ("pre-commit", re.search(r"pre[_-]commit install", b).start()),
        ("server", b.find("-m studio.server")),
    ]
    positions = [p for _, p in order]
    assert all(p != -1 for p in positions), f"有步驟缺漏：{order}"
    assert positions == sorted(positions), f"步驟順序不正確：{order}"


# ---- pre-commit 標『選填』 ----
def test_pre_commit_marked_optional():
    b = _happy_block()
    pc_line = next(ln for ln in b.splitlines() if re.search(r"pre[_-]commit install", ln))
    assert "選填" in pc_line, f"pre-commit install 未標『選填』：{pc_line}"


# ---- 套件指令一律 .venv/bin/python3 -m pip，流程內無裸 pip install ----
def test_no_bare_pip_in_block():
    b = _happy_block()
    # 逐行去註解後檢查：凡含 pip install 的行，必須是 `-m pip install` 形式
    for ln in b.splitlines():
        code = ln.split("#", 1)[0]
        if "pip install" in code:
            assert "-m pip install" in code, f"happy-path 出現裸 pip install（應用 -m pip）：{ln}"


# ---- 複雜旗標不在 happy-path 流程展開（標準 6）：以連結指向設定表 ----
def test_links_to_settings_not_expand_flags():
    seg = SEC[SEC.find("happy-path") :]
    assert "[設定](#設定)" in seg, "happy-path 區塊未以連結指向『[設定](#設定)』表"
    assert "TI_AUTOPILOT_" not in re.sub(
        r"<!--.*?-->", "", seg, flags=re.DOTALL
    ), "happy-path 不應展開 TI_AUTOPILOT_* 旗標細節"
