"""QA 驗證：任務 #1 uvicorn 版本下限鎖定 >=0.30（保留 standard extra）。

對應驗收標準：
1. pyproject.toml 宣告 uvicorn[standard]>=0.30（standard extra 保留）
2. 改動處有註解，提及 ProxyHeaders / 最左值偽造
3. pip show uvicorn 安裝版本 >= 0.30
4. pyproject.toml 為合法 TOML，可解析
5. （由整體測試套件覆蓋）
另：架構決策要求 ci.yml 兩處同步加版本約束，避免列舉式安裝架空鎖版。
"""

import re
import subprocess
import sys

from _repo import REPO_ROOT

try:
    import tomllib  # py3.11+
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib

ROOT = REPO_ROOT
PYPROJECT = ROOT / "pyproject.toml"
CI = ROOT / ".github" / "workflows" / "ci.yml"


def _parse_version(v: str) -> tuple[int, ...]:
    nums = re.findall(r"\d+", v)
    return tuple(int(x) for x in nums[:3])


def _uvicorn_dep() -> str:
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    deps = data["project"]["dependencies"]
    matches = [d for d in deps if d.lower().lstrip().startswith("uvicorn")]
    assert len(matches) == 1, f"應恰好一條 uvicorn 依賴，實得：{matches}"
    return matches[0]


def test_acceptance4_valid_toml():
    """驗收4：合法 TOML，可被解析。"""
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    assert "project" in data and "dependencies" in data["project"]


def test_acceptance1_lower_bound_030_and_standard_extra():
    """驗收1：uvicorn[standard]>=0.30，standard extra 保留。"""
    dep = _uvicorn_dep()
    assert "[standard]" in dep, f"必須保留 standard extra：{dep}"
    m = re.search(r">=\s*([0-9]+\.[0-9]+(?:\.[0-9]+)?)", dep)
    assert m, f"必須有 >= 下限：{dep}"
    assert _parse_version(m.group(1)) >= (0, 30), f"下限需 >=0.30：{dep}"
    # 確認不是殘留的 0.27
    assert "0.27" not in dep, f"不應殘留 0.27 下限：{dep}"


def test_acceptance2_comment_mentions_proxyheaders():
    """驗收2：改動處註解提及 ProxyHeaders / 最左值偽造。"""
    text = PYPROJECT.read_text(encoding="utf-8")
    lines = text.splitlines()
    idx = next(i for i, ln in enumerate(lines) if "uvicorn[standard]" in ln)
    # 取該行往上連續的註解區塊
    block = []
    j = idx - 1
    while j >= 0 and lines[j].strip().startswith("#"):
        block.append(lines[j])
        j -= 1
    comment = "\n".join(block)
    assert "ProxyHeaders" in comment, f"註解需提及 ProxyHeaders：\n{comment}"
    assert "最左值" in comment or "X-Forwarded-For" in comment, (
        f"註解需提及最左值/X-Forwarded-For 偽造：\n{comment}"
    )


def test_acceptance3_installed_version_ge_030():
    """驗收3：實際安裝的 uvicorn >= 0.30。"""
    out = subprocess.run(
        [sys.executable, "-c", "import uvicorn; print(uvicorn.__version__)"],
        capture_output=True,
        text=True,
    )
    assert out.returncode == 0, f"import uvicorn 失敗：{out.stderr}"
    ver = out.stdout.strip()
    assert _parse_version(ver) >= (0, 30), f"安裝版本需 >=0.30，實得 {ver}"


def test_installed_version_respects_upper_bound():
    """架構決策：上限 <0.36，已解析環境不應超出（避免假通過）。"""
    dep = _uvicorn_dep()
    m = re.search(r"<\s*([0-9]+\.[0-9]+(?:\.[0-9]+)?)", dep)
    if not m:
        return  # 無上限則略過
    upper = _parse_version(m.group(1))
    out = subprocess.run(
        [sys.executable, "-c", "import uvicorn; print(uvicorn.__version__)"],
        capture_output=True,
        text=True,
    )
    ver = _parse_version(out.stdout.strip())
    assert ver < upper, (
        f"安裝版本 {out.stdout.strip()} 違反 pyproject 上限 <{m.group(1)}；"
        f"需執行 pip install -e . --upgrade 重新解析"
    )


def test_ci_sync_version_constraint():
    """架構決策：ci.yml 兩處 uvicorn 安裝需帶 >=0.30 約束，避免架空鎖版。"""
    if not CI.exists():
        return
    text = CI.read_text(encoding="utf-8")
    bare = re.findall(r'"uvicorn\[standard\]"(?!\s*>=)', text)
    assert not bare, "ci.yml 仍有無版本約束的 uvicorn[standard] 列舉安裝"
    # 守住「有 0.3x 下限約束」的原意、不再釘死小版號（下限已升至 0.31，見 issue #0001）。
    pinned = re.findall(r"uvicorn\[standard\]>=0\.3\d", text)
    assert len(pinned) >= 2, f"ci.yml 兩處皆需鎖版，實得 {len(pinned)} 處"
