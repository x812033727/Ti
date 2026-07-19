"""QA：任務 #4 禁改 marker 的 prompt 與文件契約。"""

from __future__ import annotations

from pathlib import Path

from studio.orchestrator import AGENDA_PROMPT_RULES

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS_DOC = (ROOT / "docs" / "workflows.md").read_text(encoding="utf-8")


def test_agenda_prompt_teaches_forbidden_marker_format_and_example():
    """PM 拆任務 prompt 必須教會固定 marker 格式與一行範例。"""
    assert "`禁改: #<id> <pattern>[, <pattern>...]`" in AGENDA_PROMPT_RULES
    assert "逗號分隔多個 pattern" in AGENDA_PROMPT_RULES
    assert "範例：`禁改: #2 studio/config.py, docs/`" in AGENDA_PROMPT_RULES


def test_agenda_prompt_states_forbidden_marker_matching_semantics():
    """比對語意在 prompt 內不可只留給文件，PM 需當場看到。"""
    assert "`/` 結尾＝目錄前綴比對" in AGENDA_PROMPT_RULES
    assert "其餘為 PurePath.match 比對" in AGENDA_PROMPT_RULES
    assert "`*` 不跨 `/`" in AGENDA_PROMPT_RULES


def test_workflows_doc_records_forbidden_marker_contract():
    """文件需載明格式、範例與比對語意，避免後續引入不相容語法。"""
    assert "## 禁改路徑（`禁改:` marker）" in WORKFLOWS_DOC
    assert "禁改: #<id> <pattern>[, <pattern>...]" in WORKFLOWS_DOC
    assert "禁改: #2 studio/config.py, docs/" in WORKFLOWS_DOC
    assert "懸空 id 會被安全丟棄" in WORKFLOWS_DOC
    assert "目錄前綴比對：staged 路徑以此字串開頭即命中" in WORKFLOWS_DOC
    assert "`PurePath.match` 比對" in WORKFLOWS_DOC
    assert "`*` 不跨 `/`" in WORKFLOWS_DOC
    assert "不引入 `pathspec` 等外部依賴" in WORKFLOWS_DOC


def test_purepath_match_semantics_white_and_black():
    """確認 PurePath.match 的黑白樣本符合文件宣稱（`*` 不跨 `/`）。

    white：*.py 命中 studio/config.py（`*` 只比對最後一段 config.py）。
    black：docs/* 不命中 docs/a/b.md（`*` 只比對一段，無法同時消化 a 與 b.md）。
    """
    from pathlib import PurePath

    assert PurePath("studio/config.py").match("*.py"), "white sample 應命中"
    assert not PurePath("docs/a/b.md").match("docs/*"), "black sample 不應命中"
