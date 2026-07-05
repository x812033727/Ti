# Breaking Changes 三方同步 — 靜默漂移封口驗證（任務 #2）

> **定性**：本文件為**驗證封口**產出，非新增內容。逐字交叉核對「CHANGELOG heading／四要素順序
> marker／`TI_REQUIRE_CHOWN=warn/off` token」與 `release_note.BREAKING_HEADING` 抽取錨點及各 parser
> 是否一致，並以**實跑 mutation** 證明任一處漂移都會被契約測試在 **assertion** 攔下（非 import/collection
> error 的假綠）。**零 production code 變更、可逆。**

## 1. 同步鏈與唯一事實來源（SSOT）

資料流固定為：**CHANGELOG.md（內容 SSOT）→ `release_note.extract_breaking_block`（錨＝`BREAKING_HEADING`）
→ `publish_release.render_release_body` → `body.md`**。抽取錨點的唯一事實來源是
`studio/release_note.py:49` 的常數 `BREAKING_HEADING`；依賴方向固定為「**CHANGELOG heading 對齊常數**」，
若查出漂移一律改 `CHANGELOG.md`，不得反向改常數（架構決策）。

## 2. 逐字比對證據（具名出處，供接手者重驗）

### 2.1 Heading 錨點字串

| 角色 | 出處 | 逐字內容 |
|------|------|----------|
| 抽取錨點 SSOT（常數） | `studio/release_note.py:49` | `BREAKING_HEADING = "## ⚠️ Breaking Changes"` |
| 抽取正則（引用常數，非另寫字面值） | `studio/release_note.py:52` | `_HEADING_RE = re.compile(r"(?m)^" + re.escape(BREAKING_HEADING) + r"[ \t]*$")` |
| CHANGELOG 實際頂層 heading 行 | `CHANGELOG.md:9` | `## ⚠️ Breaking Changes` |
| 契約測試 golden value（**唯一允許的獨立字面值**） | `tests/autopilot/test_release_note_heading_contract.py:28` | `EXPECTED_HEADING = "## ⚠️ Breaking Changes"` |

三處字面值**逐字相符**（含 emoji `⚠️` U+26A0 U+FE0F、兩個 `#`、單一空白）。`release_note.py` docstring
（L12–21）規定除該 golden value 外，禁止在他處再寫一份 `"## ⚠️ Breaking Changes"` 字面值——
抽取／比對端一律 `from studio.release_note import BREAKING_HEADING`。

### 2.2 四要素順序 marker

parser `four_elements_in_order`（`tests/autopilot/test_release_note_breaking.py:94-112`）以**首次命中 index
嚴格遞增**判定，而非逐字比對整段（改字不紅、調換順序才紅）。各要素 marker 對應：

| 要素 | CHANGELOG 出處 | parser 命中依據（regex 摘要） |
|------|----------------|-------------------------------|
| ① 行為變動 | `CHANGELOG.md:15` `**① 行為變動**…已改為 `strict` 預設` | `strict…預設` / `預設…strict` / `已改為…strict` |
| ② 原因 | `CHANGELOG.md:18` `**② 原因**…symlink…root-only` | `symlink` / `root-only` |
| ③ before/after 遷移 | `CHANGELOG.md:20` `**③ before / after 遷移範例**` | `遷移` / `before\s*/\s*after` |
| ④ 生效版本 | `CHANGELOG.md:39` `**④ 生效版本**：自 `0.2.0` 起生效` | `生效版本` / `生效` / `自 X.Y.Z…起` |

四者在 CHANGELOG 的實際 index 為 L15 < L18 < L20 < L39，滿足 parser 的嚴格遞增契約。

### 2.3 `TI_REQUIRE_CHOWN=warn/off` 逃生艙 token

parser `has_warn_escape_hatch`（`tests/autopilot/test_release_note_breaking.py:123-132`）以精準 regex
`TI_REQUIRE_CHOWN\s*=\s*warn` 與 `…=\s*off` 匹配（非裸 substring，避免被 `warning` 假命中），
並要求「非 root」語境同時存在。CHANGELOG 對應 token：

| token | CHANGELOG 出處 |
|-------|----------------|
| `TI_REQUIRE_CHOWN=warn` | `CHANGELOG.md:33`、`CHANGELOG.md:42` |
| `TI_REQUIRE_CHOWN=off` | `CHANGELOG.md:35`、`CHANGELOG.md:42` |
| 非 root 語境 | `CHANGELOG.md:42` `若為**非 root** 環境部署…` |

語氣為「**使用者側逃生艙、即刻生效**」（`strict` 已是當前預設），**未**引入 deprecation 過渡期／
未來版本才 enforce 措辭——`has_future_enforce_timeline`（同檔 L140-146）守此邊界。

## 3. Mutation 實證（證明真鑑別力，非靜態假綠）

方法：備份 `CHANGELOG.md` 至 `$TMPDIR` → 對真檔施加單點漂移 → 實跑對應契約測試 → 還原 → `git diff` 驗淨。
**每個 mutation 的失敗都落在 content 層 `AssertionError`（具名 parser），非 import/collection error。**

| # | 施加的漂移 | 翻紅測試（`AssertionError`） | 命中 parser |
|---|-----------|------------------------------|-------------|
| A | 頂層 heading `## ⚠️ Breaking Changes` → `## Breaking`（去 emoji＋改名） | `test_breaking_block_at_top`、`test_changelog_contains_contract_heading`、`test_extract_breaking_block_real_changelog`、兩黑樣本前置斷言（共 5 紅） | `breaking_is_at_top` ✅、heading 契約 |
| B | 於區塊頂端注入「生效」字樣，使 `④ 生效版本` index 前移 | `test_four_elements_in_order`（L277 `AssertionError`） | `four_elements_in_order` ✅ |
| C | 移除 `TI_REQUIRE_CHOWN=warn/off` token（收斂為 `=strict`） | `test_warn_escape_hatch_present`（L287 `AssertionError`） | `has_warn_escape_hatch` ✅ |

- Mutation A 的失敗判準符合架構決策要求：「**必失敗於 `four_elements_in_order` 或 `breaking_is_at_top`**」——
  實際命中 `breaking_is_at_top`（`test_breaking_block_at_top`），且全部為 `AssertionError`，**無** typo 導致的
  import/collection error。
- 三次 mutation 後皆以 `cp "$TMPDIR/CHANGELOG.bak" CHANGELOG.md` 還原，`git diff --stat CHANGELOG.md`
  為空（exit 0），真檔零殘留。

> 註（避坑記錄）：mutation 腳本中的 lookbehind/lookahead（`(?<!\w)`）在 bash heredoc 會觸發 history
> expansion 把 `!` 轉義成 `\!` 而炸掉 regex——已改用純字串替換避開，勿在 heredoc 內寫含 `!` 的 regex。

## 4. 結論與重驗指令

**證無靜默漂移**：heading／四要素順序／warn-off token 三處與 `BREAKING_HEADING` 錨點及各 parser 逐字一致；
任一被改都會被契約測試在 assertion 攔下。接手者可用以下指令重驗（全綠 = 無漂移）：

```bash
# 現況封口驗證（應 38 passed）
.venv/bin/python -m pytest \
  tests/autopilot/test_release_note_breaking.py \
  tests/autopilot/test_release_note_heading_contract.py -q
```

重跑第 3 節任一 mutation（改真檔前務必先 `cp CHANGELOG.md "$TMPDIR/CHANGELOG.bak"`，跑完 `cp` 回還原並
`git diff --stat` 驗淨），即可親眼確認對應 parser 翻紅——**這是靜態「看兩處相等」無法給的鑑別力保證**。
