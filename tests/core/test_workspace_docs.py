"""知識沉澱地基（workspace.append_doc / read_doc_tail）的純 IO 測試。

涵蓋：append 累積與標頭、白名單外檔名拒絕、路徑穿越拒絕、尾段截斷（段落邊界）、
不存在檔案回空字串，以及 docs/ 知識檔屬交付物（會出現在 list_files）。
"""

from __future__ import annotations

import pytest

from studio import config, workspace


@pytest.fixture(autouse=True)
def _ws_root(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "WORKSPACE_ROOT", tmp_path / "ws")


def test_append_and_read_roundtrip():
    workspace.create_workspace("w1")
    workspace.append_doc("w1", "RESEARCH.md", "重點: 用 FastAPI\n建議: 搭配 SQLite")
    text = workspace.read_doc_tail("w1", "RESEARCH.md", 4000)
    assert "用 FastAPI" in text
    assert text.startswith("## ")  # 每段帶時間標頭，利跨場次追溯

    # append 模式：第二段追加、不覆寫第一段
    workspace.append_doc("w1", "RESEARCH.md", "重點: 第二場補充")
    text = workspace.read_doc_tail("w1", "RESEARCH.md", 4000)
    assert "用 FastAPI" in text and "第二場補充" in text


def test_docs_are_deliverables_in_list_files():
    workspace.create_workspace("w2")
    workspace.append_doc("w2", "DECISIONS.md", "設計決策: 用 SQLite")
    assert "docs/DECISIONS.md" in workspace.list_files("w2")


def test_whitelist_rejects_other_names():
    workspace.create_workspace("w3")
    workspace.append_doc("w3", "EVIL.md", "不該被寫入")
    workspace.append_doc("w3", "../escape.md", "不該被寫入")
    root = workspace.workspace_path("w3")
    assert not (root / "docs" / "EVIL.md").exists()
    assert not (root.parent / "escape.md").exists()
    assert workspace.read_doc_tail("w3", "EVIL.md", 100) == ""
    assert workspace.read_doc_tail("w3", "../escape.md", 100) == ""


def test_empty_text_and_missing_file():
    workspace.create_workspace("w4")
    workspace.append_doc("w4", "PRD.md", "   ")  # 空白忽略
    assert not (workspace.workspace_path("w4") / "docs").exists() or workspace.read_doc_tail(
        "w4", "PRD.md", 100
    ) == ""
    assert workspace.read_doc_tail("w4", "RESEARCH.md", 100) == ""
    assert workspace.read_doc_tail("w4", "RESEARCH.md", 0) == ""  # max_chars<=0 視為停用


def test_tail_truncates_at_paragraph_boundary():
    workspace.create_workspace("w5")
    old = "舊段落甲" * 50
    new = "新段落乙" * 10
    workspace.append_doc("w5", "RESEARCH.md", old)
    workspace.append_doc("w5", "RESEARCH.md", new)
    tail = workspace.read_doc_tail("w5", "RESEARCH.md", len(new) + 40)
    # 超長時取尾段，且從段落（空行）邊界起切，不腰斬句子——切完正好從新段標頭開始。
    assert "新段落乙" in tail
    assert len(tail) <= len(new) + 40
    assert tail.startswith("## ")
