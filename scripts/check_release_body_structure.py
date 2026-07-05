"""任務 #2：對 v0.2.0 線上 release body 做「結構斷言」核對（一次性核對腳本，輸出為產物）。

依架構決策，本腳本是**一次性人工/腳本動作**、其輸出為證據產物，**不進 CI 測試套件**
（避免把打 live GitHub 的網路測試綁進 CI；且逐字比對受 GitHub 換行正規化影響易假性不符）。
它讀取任務 #1 已抓好的雙來源證據檔 `docs/evidence/release-v0.2.0-online-body.json`
（zero 網路、確定性），做下列核對：

  1. 雙來源交叉驗證：`gh release view --json body`（主）與 REST
     `GET /repos/.../releases/tags/v0.2.0` 的 `body`（第二來源），各自
     `sed 's/\\r$//'`（CRLF→LF）＋去尾空行正規化後必須逐字相等。
  2. 結構斷言（避開換行坑，不做整份逐字 diff）：
     - 頂部第一個頂層 `## ` 區塊逐字為 `## ⚠️ Breaking Changes`（引用 release_note.BREAKING_HEADING 常數，不硬寫）。
     - 四要素齊：① 行為變動 ② 原因 ③ before/after ④ 生效版本。
     - ④ 生效版本逐字對應 `自 0.2.0 起`（版本走 pyproject SSOT，不硬寫任意版本）。
     - 含逃生艙字串 `TI_REQUIRE_CHOWN=warn/off`。

用法：
    .venv/bin/python scripts/check_release_body_structure.py
退出碼 0＝全部核對通過；非 0＝任一項不符（並印出具名問題）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from studio.release_note import BREAKING_HEADING, pyproject_version

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "docs" / "evidence" / "release-v0.2.0-online-body.json"


def normalize(text: str) -> str:
    """CRLF→LF（``sed 's/\\r$//'`` 等效）＋去每行尾隨空白＋去尾端空行。

    GitHub 儲存/回傳 release body 常做行尾正規化，直接 diff 會假性不符；
    先正規化再比對，避開換行坑。
    """
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    lines = [ln.rstrip() for ln in lines]
    # 去尾端空行
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines)


def first_top_level_h2(body: str) -> str | None:
    """回傳 body 中第一個頂層 `## ` 行（去尾隨空白）；無則 None。

    只認整行以 ``## `` 起頭者（頂層 h2），`### ` 等更深標題不算。
    """
    for raw in body.split("\n"):
        ln = raw.rstrip()
        if ln.startswith("## "):
            return ln
    return None


# 四要素偵測：圈號錨＋語意關鍵字皆須命中（與 tests/autopilot/_release_check.py 同一把尺理念）。
FOUR_ELEMENTS = (
    ("① 行為變動", "① 行為變動", ("strict",)),
    ("② 原因", "② 原因", ("symlink", "root")),
    ("③ before/after", "③ before / after", ("之前", "之後")),
    ("④ 生效版本", "④ 生效版本", ("自",)),
)


def check(evidence: dict, version: str) -> list[str]:
    """回傳問題清單；空 list＝全部核對通過。"""
    problems: list[str] = []

    gh_body_raw = evidence["gh_release_view"]["body"]
    rest_body_raw = evidence["rest_release_by_tag_subset"]["body"]

    gh = normalize(gh_body_raw)
    rest = normalize(rest_body_raw)

    # 1) 雙來源交叉驗證（正規化後逐字相等）
    if gh != rest:
        problems.append("雙來源不一致：gh release view body 與 REST body 正規化後不相等")

    # 結構斷言全對「主來源 gh body（正規化後）」進行
    body = gh

    # 2a) 頂部第一個頂層 `## ` 區塊逐字為 BREAKING_HEADING
    first_h2 = first_top_level_h2(body)
    if first_h2 is None:
        problems.append("body 無任何頂層 `## ` 區塊")
    elif first_h2 != BREAKING_HEADING:
        problems.append(
            f"頂部第一個 `## ` 區塊非 Breaking：期望 {BREAKING_HEADING!r}，實得 {first_h2!r}"
        )

    # 2b) 四要素齊
    for name, anchor, semantics in FOUR_ELEMENTS:
        if anchor not in body:
            problems.append(f"四要素缺圈號錨：{name}")
            continue
        if not any(kw.lower() in body.lower() for kw in semantics):
            problems.append(f"四要素 {name} 圈號在但語意關鍵字缺（{semantics}）")

    # 2c) ④ 生效版本逐字對應 `自 <version> 起`（版本走 pyproject SSOT）
    effective = f"自 `{version}` 起"
    effective_plain = f"自 {version} 起"
    if effective not in body and effective_plain not in body:
        problems.append(f"④ 生效版本未逐字對應：找不到 {effective!r} 或 {effective_plain!r}")

    # 2d) 逃生艙字串
    if "TI_REQUIRE_CHOWN=warn" not in body or "TI_REQUIRE_CHOWN=off" not in body:
        problems.append("逃生艙缺：body 未同時含 `TI_REQUIRE_CHOWN=warn` 與 `TI_REQUIRE_CHOWN=off`")

    # 佐證 task#1 已標記雙來源一致
    if not evidence.get("body_match"):
        problems.append("證據檔 body_match 非 true（task#1 抓取階段已標記雙來源不符）")

    return problems


def main() -> int:
    if not EVIDENCE.exists():
        print(f"FAIL：缺證據檔 {EVIDENCE}", file=sys.stderr)
        return 2
    evidence = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    version = pyproject_version()

    print(f"== v0.2.0 線上 body 結構斷言核對 ==")
    print(f"證據檔：{EVIDENCE.relative_to(ROOT)}")
    print(f"pyproject 版本（SSOT）：{version}")
    print(f"Breaking heading 常數：{BREAKING_HEADING!r}")

    problems = check(evidence, version)

    first_h2 = first_top_level_h2(normalize(evidence["gh_release_view"]["body"]))
    print(f"頂部第一個頂層 `## ` 區塊：{first_h2!r}")

    if problems:
        print("\n核對未通過：")
        for p in problems:
            print(f"  - {p}")
        return 1

    print("\n核對通過（雙來源一致＋頂部 Breaking 置頂＋四要素齊＋逃生艙齊＋生效版本逐字對應）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
