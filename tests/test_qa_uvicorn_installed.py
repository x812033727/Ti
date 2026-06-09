"""QA 驗證：任務 #3 重新解析環境依賴，確認 uvicorn 實際安裝版本 ≥ 0.30。

驗收標準 3：`pip show uvicorn` 顯示已安裝版本 ≥ 0.30。
做法：
  - 以 importlib.metadata 讀取已安裝版本（等同 pip show 的 Version 欄位），確認 ≥0.30。
  - 與 pyproject 宣告交叉驗證：安裝版本須落在 [下限, 上限) 區間，避免假通過。
  - 確認 standard extra 帶入的執行期相依（uvloop / httptools / websockets）已安裝。
"""

import re
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

try:
    import tomllib  # py3.11+
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib

ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = ROOT / "pyproject.toml"


def _ver_tuple(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3])


def _installed_uvicorn() -> str:
    try:
        return version("uvicorn")
    except PackageNotFoundError as err:  # pragma: no cover
        raise AssertionError("uvicorn 未安裝；需先 pip install -e . --upgrade") from err


def _uvicorn_dep() -> str:
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    deps = data["project"]["dependencies"]
    m = [d for d in deps if d.lower().startswith("uvicorn")]
    assert len(m) == 1, f"應恰好一條 uvicorn 依賴：{m}"
    return m[0]


def test_installed_version_ge_030():
    """驗收3：已安裝版本 ≥ 0.30。"""
    ver = _installed_uvicorn()
    assert _ver_tuple(ver) >= (0, 30), f"已安裝 uvicorn {ver} 未達 >=0.30"


def test_installed_version_within_declared_range():
    """交叉驗證：安裝版本落在 pyproject 宣告的 [下限, 上限) 內，排除假通過。"""
    ver = _ver_tuple(_installed_uvicorn())
    dep = _uvicorn_dep()
    low = re.search(r">=\s*([\d.]+)", dep)
    high = re.search(r"<\s*([\d.]+)", dep)
    assert low, f"宣告缺少 >= 下限：{dep}"
    assert ver >= _ver_tuple(low.group(1)), f"安裝版本 {ver} 低於宣告下限 {low.group(1)}"
    if high:
        assert ver < _ver_tuple(high.group(1)), (
            f"安裝版本 {ver} 違反宣告上限 <{high.group(1)}；需 pip install -e . --upgrade 重新解析"
        )


def test_uvicorn_importable_and_matches_metadata():
    """import 的 __version__ 與 metadata 一致（環境一致性）。"""
    import uvicorn

    assert _ver_tuple(uvicorn.__version__) == _ver_tuple(_installed_uvicorn())


def test_standard_extra_runtime_deps_present():
    """standard extra 必須帶入執行期相依。"""
    missing = []
    for pkg in ("uvloop", "httptools", "websockets"):
        try:
            version(pkg)
        except PackageNotFoundError:
            missing.append(pkg)
    assert not missing, f"standard extra 相依缺失：{missing}"
