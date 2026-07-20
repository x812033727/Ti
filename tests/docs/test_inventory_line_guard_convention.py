from __future__ import annotations

import re
from pathlib import Path

from _repo import REPO_ROOT

DOCS_DIR = REPO_ROOT / "studio" / "docs"
CONVENTION = DOCS_DIR / "inventory_line_guard_convention.md"

VALID_TYPES = {"line-number", "marker-only", "historical-location"}
VALID_STATUSES = {"active", "planned", "not-required"}
PY_LINE_REF_RE = re.compile(r"[A-Za-z0-9_./-]+\.py:\d+")
HISTORICAL_LINE_RE = re.compile(r"\bL\d+(?:-\d+)?\b")


def _inventory_docs() -> list[Path]:
    return sorted(DOCS_DIR.glob("*_inventory.md"))


def _section(text: str) -> str:
    match = re.search(r"^## 行號守門\n(?P<body>.*?)(?=^## |\Z)", text, re.M | re.S)
    assert match, "inventory 必須有 `## 行號守門` metadata 區塊"
    return match.group("body")


def _metadata(path: Path) -> dict[str, str]:
    body = _section(path.read_text(encoding="utf-8"))
    data: dict[str, str] = {}
    for line in body.splitlines():
        match = re.match(r"^- (?P<key>類型|狀態|守門測試|模板|原則)：(?P<value>.+)$", line)
        if match:
            data[match.group("key")] = match.group("value").strip()
    return data


def _code_value(value: str) -> str:
    match = re.fullmatch(r"`([^`]+)`", value)
    return match.group(1) if match else value


def _slug(path: Path) -> str:
    stem = path.stem
    assert stem.endswith("_inventory"), f"inventory 檔名須以 `_inventory` 結尾：{path.name}"
    return stem.removesuffix("_inventory")


def test_convention_doc_defines_naming_and_template() -> None:
    text = CONVENTION.read_text(encoding="utf-8")

    assert "tests/docs/test_inventory_line_guard_<inventory-slug>.py" in text
    assert "文件只作被校驗方" in text
    assert "實碼動態重算行號" in text
    assert "AST 或唯一字串" in text
    assert "不得為了過測試改產品碼或新增 wrapper" in text
    assert "def test_inventory_line_numbers_match_live_code" in text


def test_all_studio_inventory_docs_declare_line_guard_metadata() -> None:
    docs = _inventory_docs()
    assert docs, "studio/docs 應至少有一份 inventory"

    for path in docs:
        data = _metadata(path)
        missing = {"類型", "狀態", "守門測試", "模板", "原則"} - data.keys()
        assert not missing, f"{path.name} 缺少 metadata 欄位：{sorted(missing)}"

        inv_type = _code_value(data["類型"])
        status = _code_value(data["狀態"])
        template = _code_value(data["模板"])

        assert inv_type in VALID_TYPES, f"{path.name} 類型不合法：{inv_type}"
        assert status in VALID_STATUSES, f"{path.name} 狀態不合法：{status}"
        assert template == "studio/docs/inventory_line_guard_convention.md"
        assert "文件只作被校驗方" in data["原則"] or inv_type != "line-number"


def test_line_number_inventory_uses_official_guard_test_name() -> None:
    for path in _inventory_docs():
        text = path.read_text(encoding="utf-8")
        data = _metadata(path)
        inv_type = _code_value(data["類型"])
        status = _code_value(data["狀態"])
        guard = _code_value(data["守門測試"])
        expected_guard = f"tests/docs/test_inventory_line_guard_{_slug(path)}.py"

        has_python_line_refs = PY_LINE_REF_RE.search(text) is not None
        has_historical_line_refs = HISTORICAL_LINE_RE.search(text) is not None

        if has_python_line_refs:
            assert inv_type == "line-number", f"{path.name} 含現碼行號，類型須為 line-number"
        elif has_historical_line_refs:
            assert inv_type in {
                "historical-location",
                "line-number",
            }, f"{path.name} 含 L<line> 位置，須明確標為歷史或行號型"

        if inv_type == "line-number":
            assert guard == expected_guard, f"{path.name} 守門測試命名須為 {expected_guard}"
            assert status in {"active", "planned"}
            if status == "active":
                assert (REPO_ROOT / guard).exists(), f"{path.name} active guard 不存在：{guard}"
        else:
            assert status == "not-required"
            assert guard == "不適用"
