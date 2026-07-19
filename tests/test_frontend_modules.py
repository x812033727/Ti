"""前端 ES module 靜態守護：免建置環境沒有打包器攔錯，import 路徑打錯＝生產白屏。

三層守護：
1. import-graph：web/ 下所有相對 import 都解析得到實體檔案。
2. 入口契約：index.html 以 <script type="module"> 載入 /static/app.js。
3. CSS token 主題完整性：[data-theme="light"] 定義的 token 必須是 :root 的子集
   （防拼錯新增孤兒 token）；所有 var(--x) 引用都有定義（防漏定義）。
"""

from __future__ import annotations

import re

from _repo import REPO_ROOT

WEB = REPO_ROOT / "web"

_IMPORT_RE = re.compile(r"""import\s+(?:[^"']+\s+from\s+)?["'](\.[^"']+)["']""")


def _js_files():
    return [WEB / "app.js", WEB / "login.js", *sorted((WEB / "js").rglob("*.js"))]


def test_all_relative_imports_resolve():
    """web/ 內每個相對 import 都指到存在的檔案（路徑錯＝瀏覽器 404 白屏）。"""
    missing = []
    for f in _js_files():
        for spec in _IMPORT_RE.findall(f.read_text(encoding="utf-8")):
            target = (f.parent / spec).resolve()
            if not target.is_file():
                missing.append(f"{f.relative_to(REPO_ROOT)} → {spec}")
    assert not missing, "以下 import 解析不到檔案：\n" + "\n".join(missing)


def test_index_loads_app_as_module():
    """入口必須是 ES module（拆分後 app.js 全靠 import 組裝）。"""
    html = (WEB / "index.html").read_text(encoding="utf-8")
    assert re.search(
        r'<script\s+type="module"\s+src="/static/app\.js">', html
    ), 'index.html 須以 <script type="module" src="/static/app.js"> 載入入口'


def test_styles_aggregator_imports_resolve():
    """styles.css 聚合檔的每個 @import 都指到存在的檔案。"""
    css = (WEB / "styles.css").read_text(encoding="utf-8")
    targets = re.findall(r'@import\s+url\("([^"]+)"\)', css)
    assert targets, "styles.css 應為 @import 聚合檔"
    missing = [t for t in targets if not (WEB / t).is_file()]
    assert not missing, f"styles.css @import 找不到：{missing}"


def _token_names(block: str) -> set[str]:
    return set(re.findall(r"(--[a-z0-9-]+)\s*:", block))


def _extract_block(css: str, marker: str) -> str:
    idx = css.index(marker)
    open_brace = css.index("{", idx)
    depth = 0
    for i in range(open_brace, len(css)):
        if css[i] == "{":
            depth += 1
        elif css[i] == "}":
            depth -= 1
            if depth == 0:
                return css[open_brace : i + 1]
    raise AssertionError(f"{marker} 大括號不配對")


def test_light_theme_tokens_subset_of_root():
    """淺色主題只能覆寫 :root 已存在的 token（防拼錯產生孤兒 token 永遠不生效）。"""
    css = (WEB / "css" / "tokens.css").read_text(encoding="utf-8")
    root_keys = _token_names(_extract_block(css, ":root"))
    light_keys = _token_names(_extract_block(css, '[data-theme="light"]'))
    orphans = light_keys - root_keys
    assert not orphans, f"[data-theme=light] 有 :root 不存在的 token：{sorted(orphans)}"
    # 主題核心 token 必須雙邊都有（漏了會整面破版）
    core = {"--bg", "--text", "--muted", "--surface-1", "--surface-solid", "--border"}
    assert (
        core <= root_keys and core <= light_keys
    ), f"核心 token 缺漏：root 缺 {sorted(core - root_keys)}、light 缺 {sorted(core - light_keys)}"


def test_all_var_references_defined():
    """所有 var(--x) 引用在某處有定義（tokens 或元件內自訂如 --lane-c）。"""
    defined: set[str] = set()
    used: set[str] = set()
    for f in sorted((WEB / "css").glob("*.css")):
        text = f.read_text(encoding="utf-8")
        defined |= _token_names(text)
        used |= set(re.findall(r"var\((--[a-z0-9-]+)", text))
    missing = used - defined
    assert not missing, f"以下 token 有引用但無定義：{sorted(missing)}"
