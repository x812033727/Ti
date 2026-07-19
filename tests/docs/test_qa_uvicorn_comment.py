"""QA 驗證：任務 #2 改動處註解。

驗收標準 2：uvicorn 依賴改動處附有說明用途的註解，提及 ProxyHeaders / 最左值偽造。
架構決策追加：註解需同時點出
  (A) ProxyHeaders 信任鏈強化／避免 X-Forwarded-For 取最左值偽造
  (B) 版本鎖僅為基線，實際防護需設 forwarded_allow_ips、嚴禁 "*"
且註解需緊鄰依賴宣告行（之上連續註解區塊），確保語意關聯。
"""

import re
import tomllib

from _repo import REPO_ROOT

ROOT = REPO_ROOT
PYPROJECT = ROOT / "pyproject.toml"


def _dep_line_index(lines: list[str]) -> int:
    idxs = [
        i
        for i, ln in enumerate(lines)
        if "uvicorn[standard]" in ln and not ln.strip().startswith("#")
    ]
    assert len(idxs) == 1, f"應恰好一行 uvicorn 依賴宣告，實得索引：{idxs}"
    return idxs[0]


def _comment_block_above(lines: list[str], idx: int) -> str:
    block = []
    j = idx - 1
    while j >= 0 and lines[j].strip().startswith("#"):
        block.append(lines[j])
        j -= 1
    return "\n".join(reversed(block))


def test_comment_block_exists_adjacent():
    """註解必須緊鄰依賴行上方（非空）。"""
    lines = PYPROJECT.read_text(encoding="utf-8").splitlines()
    idx = _dep_line_index(lines)
    block = _comment_block_above(lines, idx)
    assert block.strip(), "依賴宣告上方必須有緊鄰的註解區塊"


def test_comment_mentions_proxyheaders_and_leftmost():
    """面向 A：ProxyHeaders 信任鏈 + X-Forwarded-For 最左值偽造。"""
    lines = PYPROJECT.read_text(encoding="utf-8").splitlines()
    block = _comment_block_above(lines, _dep_line_index(lines))
    assert "ProxyHeaders" in block, f"註解需提及 ProxyHeaders：\n{block}"
    assert "信任鏈" in block, f"註解需提及信任鏈強化：\n{block}"
    assert "X-Forwarded-For" in block, f"註解需提及 X-Forwarded-For：\n{block}"
    assert "最左值" in block, f"註解需提及取最左值偽造：\n{block}"


def test_comment_mentions_forwarded_allow_ips_landed():
    """面向 B：需設 forwarded_allow_ips、嚴禁 "*"，且註明防護已由 server.main() 落地（issue #0001）。"""
    lines = PYPROJECT.read_text(encoding="utf-8").splitlines()
    block = _comment_block_above(lines, _dep_line_index(lines))
    assert "forwarded_allow_ips" in block, f"註解需提醒設定 forwarded_allow_ips：\n{block}"
    assert ('"*"' in block) or ("嚴禁" in block), f'註解需警示嚴禁 "*"：\n{block}'
    # issue #0001 已落地：註解由「基線」改為點明防護已在 server.main() 實際生效。
    assert (
        "落地" in block or "server.main()" in block
    ), f"註解需說明防護已落地（server.main()）：\n{block}"


def test_pyproject_still_valid_toml():
    """註解不得破壞 TOML 合法性。"""
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    deps = data["project"]["dependencies"]
    # 下限已升至 0.31（issue #0001）；守住「有 uvicorn[standard]>=0.3x」而非釘死小版號。
    assert any(d.startswith("uvicorn[standard]>=0.3") for d in deps), deps


def test_comment_lines_are_proper_toml_comments():
    """註解區塊每行都是合法 # 註解（避免半行污染值）。"""
    lines = PYPROJECT.read_text(encoding="utf-8").splitlines()
    idx = _dep_line_index(lines)
    j = idx - 1
    while j >= 0 and lines[j].strip():
        if not lines[j].strip().startswith("#"):
            break
        assert re.match(r"^\s*#", lines[j]), f"第 {j + 1} 行非合法註解：{lines[j]}"
        j -= 1
