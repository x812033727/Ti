"""Release note 渲染與 Breaking Changes 區塊抽取。

Heading 字串契約（唯一事實來源）
================================
頂層 Breaking Changes 區塊的 heading **必須**逐字為 ``BREAKING_HEADING`` 所定義
的字串（``## ⚠️ Breaking Changes``）。release pipeline 將以此 heading 為錨點，從
``CHANGELOG.md`` 抽出區塊並注入 tag notes / email banner 兩個發佈出口。

注意：本模組**目前僅宣告 heading 字串契約常數**；抽取器（task #1）與 pipeline
整合（task #3，接入 ``publisher.py`` / ``deploy.py``）為後續任務，尚未實作。
上述「抽出並注入兩出口」描述的是契約的目的地，非當前已落地的行為。

為什麼鎖死成常數而非散落各處的字面值：
- 任何抽取／比對端一律 ``from studio.release_note import BREAKING_HEADING``，
  不得在他處再寫一份 ``"## ⚠️ Breaking Changes"`` 字面值。**唯一允許的例外**是
  ``test_release_note_heading_contract.py`` 裡的 golden value——沒有一份獨立字面值
  就無法驗證常數本身不漂移；該處已注釋標明此為例外。
- 若有人把 CHANGELOG 的 heading 改成 ``## Breaking`` 或拿掉 emoji，比對會立刻
  漏抓——測試 ``tests/autopilot/test_release_note_heading_contract.py`` 引用本常數
  逐字斷言 CHANGELOG 仍含此 heading，故任何漂移會在 CI 翻紅。
- 「測試 import 這個常數」本身就是 CI 強制機制：常數改名或模組搬路徑，import 爆炸，
  比 prose 文件（會靜默過時）更可靠，因此不另立 CONTRIBUTING.md 條目。

emoji（⚠️, U+26A0 U+FE0F）是契約的一部分，不可省略——抽取端會 ``re.escape``
本常數，故 emoji 與任何特殊字元都被當字面值處理。
"""

# TODO: 若 email_banner 需要 HTML multipart，將 render_email_banner 拆出獨立 renderer 模組。

from __future__ import annotations

# 頂層 Breaking Changes 區塊 heading 的唯一事實來源。
# 改動此字串＝改動發佈契約，務必同步 CHANGELOG.md 與相依測試。
BREAKING_HEADING = "## ⚠️ Breaking Changes"
