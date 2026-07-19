"""任務 #3 驗收：pre-commit install 明確列入「首次設定流程」的一步並標選填。

對應 PM 驗收標準 3：
- `pre-commit install`（或 `python -m pre_commit install`）出現在首次設定流程中。
- 標註為『選填』。
- 是流程序列中的一個明確步驟（非僅在文末『測試』段附帶提及）。
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
    m = re.search(r"happy-path.*?```bash\n(.*?)```", SEC, re.DOTALL)
    assert m, "找不到 happy-path bash code block"
    return m.group(1)


_PC_RE = re.compile(r"(?:-m\s+pre_commit|pre-commit)\s+install")


# ---- pre-commit install 出現在首次設定流程（happy-path code block）中 ----
def test_pre_commit_in_flow():
    b = _happy_block()
    assert _PC_RE.search(b), "首次設定流程中找不到 pre-commit / pre_commit install"


# ---- 該步標『選填』----
def test_pre_commit_marked_optional():
    b = _happy_block()
    line = next(ln for ln in b.splitlines() if _PC_RE.search(ln))
    assert "選填" in line, f"pre-commit install 所在步驟未標『選填』：{line}"


# ---- pre-commit 是流程中的明確一步：位於 pip install 之後、啟動 server 之前 ----
def test_pre_commit_is_a_step_in_order():
    b = _happy_block()
    pos_pc = _PC_RE.search(b).start()
    pos_install = b.find('pip install -e ".[dev]"')
    pos_server = b.find("-m studio.server")
    assert pos_install != -1 and pos_server != -1
    assert (
        pos_install < pos_pc < pos_server
    ), f"pre-commit 步驟未落在 install 與 server 之間：install={pos_install}, pc={pos_pc}, server={pos_server}"


# ---- 該步以 .venv 內 python 執行（與流程其他步一致，非系統環境）----
def test_pre_commit_uses_venv_python():
    b = _happy_block()
    line = next(ln for ln in b.splitlines() if _PC_RE.search(ln))
    assert (
        ".venv/bin/python3 -m pre_commit install" in line
    ), f"pre-commit 步驟應以 .venv/bin/python3 -m pre_commit install 執行：{line}"


# ---- pre-commit 確為專案實際工具（.pre-commit-config.yaml 存在）----
def test_pre_commit_config_exists():
    assert (ROOT / ".pre-commit-config.yaml").exists(), "專案缺 .pre-commit-config.yaml"
