"""任務 #5 驗收：全文交叉校對連結與錨點，複雜旗標只連結不展開。

對應 PM 驗收標準 6、7：
- 全文 Markdown 連結與段內錨點皆可正確跳轉，無壞連結。
  * `#anchor` 以 GitHub slug 規則對應到實際標題。
  * 相對檔案連結（CONTRIBUTING.md / ARCHITECTURE.md）對應檔案存在。
- 關鍵交叉連結到位：指向「設定」表、CONTRIBUTING.md、ARCHITECTURE.md。
- 複雜旗標（TI_AUTOPILOT_*）只在「設定」段展開，流程/門禁段只連結不展開。
"""

import re

from _repo import REPO_ROOT

ROOT = REPO_ROOT
README = (ROOT / "README.md").read_text(encoding="utf-8")

LINK_RE = re.compile(r"(?<!\!)\[([^\]]+)\]\(([^)]+)\)")


def _strip_code_fences(md: str) -> str:
    """移除 ``` 圍欄 code block，避免把區塊內的 `# 註解` 誤判為標題。"""
    out, in_fence = [], False
    for ln in md.splitlines():
        if ln.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if not in_fence:
            out.append(ln)
    return "\n".join(out)


def _slugify(title: str) -> str:
    """GitHub 風格 anchor slug：小寫、移除標點（保留 CJK/字母數字/底線）、空格轉連字號。"""
    s = title.strip().lower()
    s = re.sub(r"[^\w\s-]", "", s, flags=re.UNICODE)  # 去標點/emoji，保留 \w 與空白/連字號
    s = s.replace(" ", "-")
    return s


BODY = _strip_code_fences(README)
HEADINGS = [
    re.sub(r"^#{1,6}\s+", "", ln) for ln in BODY.splitlines() if re.match(r"^#{1,6}\s", ln)
]
SLUGS = {_slugify(h) for h in HEADINGS}
LINKS = LINK_RE.findall(README)


# ---- 所有段內錨點連結都能對應到實際標題 ----
def test_all_anchor_links_resolve():
    broken = []
    for text, target in LINKS:
        if target.startswith("#"):
            anchor = target[1:]
            if anchor not in SLUGS:
                broken.append((text, target))
    assert not broken, f"壞錨點（找不到對應標題）：{broken}\n可用 slug：{sorted(SLUGS)}"


# ---- 相對檔案連結對應的檔案真實存在 ----
def test_relative_file_links_exist():
    missing = []
    for text, target in LINKS:
        if target.startswith(("#", "http://", "https://", "mailto:")):
            continue
        path = target.split("#", 1)[0]  # 去掉檔內錨點
        if not path:
            continue
        if not (ROOT / path).exists():
            missing.append((text, target))
    assert not missing, f"連結指向不存在的檔案：{missing}"


# ---- 關鍵交叉連結到位：設定表 ----
def test_settings_anchor_link_present():
    assert any(t == "#設定" for _, t in LINKS), "全文缺指向『[設定](#設定)』表的連結"
    assert "設定" in SLUGS, "缺『## 設定』標題（錨點無法跳轉）"


# ---- 關鍵交叉連結到位：CONTRIBUTING.md / ARCHITECTURE.md ----
def test_doc_file_links_present():
    targets = {t for _, t in LINKS}
    assert "CONTRIBUTING.md" in targets, "缺指向 CONTRIBUTING.md 的連結"
    assert "ARCHITECTURE.md" in targets, "缺指向 ARCHITECTURE.md 的連結"
    assert (ROOT / "CONTRIBUTING.md").exists()
    assert (ROOT / "ARCHITECTURE.md").exists()


# ---- 門禁 (B) 連到的『Autopilot 安全旗標補充』錨點可跳轉 ----
def test_autopilot_supplement_anchor_resolves():
    assert any(t == "#autopilot-安全旗標補充" for _, t in LINKS), (
        "缺指向『Autopilot 安全旗標補充』小節的連結"
    )
    assert "autopilot-安全旗標補充" in SLUGS, "『Autopilot 安全旗標補充』錨點無法解析"


# ---- 複雜旗標只連結不展開：TI_AUTOPILOT_* 完整變數名只在『設定』段出現 ----
def test_autopilot_flags_only_expanded_in_settings():
    # 全程用原始 README 的字元位置，邏輯才一致。
    # 擷取 ## 設定 段（含其下 #### 子節，到下一個 ## 之前）的位置區間
    m = re.search(r"^##\s+設定\s*$.*?(?=^##\s)", README, re.MULTILINE | re.DOTALL)
    assert m, "找不到『## 設定』段"
    seg_start, seg_end = m.start(), m.end()
    # 註解區間（維護提醒中刻意提及變數名，需排除）
    comment_spans = [(c.start(), c.end()) for c in re.finditer(r"<!--.*?-->", README, re.DOTALL)]

    def in_comment(p: int) -> bool:
        return any(a <= p < b for a, b in comment_spans)

    for var in ("TI_AUTOPILOT_FORCE_PUSH", "TI_AUTOPILOT_MERGE_ADMIN"):
        occurrences = [mm.start() for mm in re.finditer(re.escape(var), README)]
        real = [p for p in occurrences if not in_comment(p)]
        assert real, f"README 不再含 {var}（非註解處）"
        for pos in real:
            assert seg_start <= pos < seg_end, (
                f"{var} 出現在『設定』段之外（pos={pos}，段範圍={seg_start}..{seg_end}）"
                "——複雜旗標不應在流程/門禁段展開"
            )
