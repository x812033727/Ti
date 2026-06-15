"""QA 任務 #4：鎖死 release note（CHANGELOG.md）的 Breaking change 契約。

對應驗收標準 #1~#7：
  1. repo 根目錄存在 release note 檔，含明確版本字串（與 pyproject 單一事實來源一致）。
  2. Breaking change 以獨立區塊置於最頂端，標籤明確（⚠️ / Breaking Changes），
     且位置先於 ### Added / ### Changed 等一般區塊。
  3. TI_REQUIRE_CHOWN 條目四要素齊備且順序固定：
     ①行為變動（strict 預設）②安全原因（symlink/root-only）③before/after 遷移 ④生效版本。
  4. 過渡步驟明示：非 root 設 warn 或 off，錯誤值 fail-safe 回退 strict。
  5. 可追的遷移指引位置（指向 README「state 安全寫入」小節）。
  6. 反向黑樣本：缺區塊／缺字樣應 fail，證明真鑑別力。
  7. 與 README／.env.example 三態語意、版本字串無矛盾。

設計依架構決策：
  - 版本字串以 pyproject.toml 為單一事實來源，用 tomllib 讀，不硬寫。
  - 反向黑樣本以 in-memory 字串截斷，不動真實檔案、不寫磁碟。
  - 四要素以 index 比相對順序，不逐字比對整段（改字不紅、調換順序才紅）。
  - README 互指只斷言 raw text「state 安全寫入」一致，不追 HTML anchor hash。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

# 直接 import 模組的純函式與唯一事實來源常數：
#   - 此 import 本身即構成 CI 強制契約（常數改名／模組搬路徑→import 爆炸）。
#   - pyproject_version 統一用模組版本，不在測試內另寫平行副本（避免靜默分歧）。
from studio.release_note import (
    BREAKING_HEADING,
    extract_breaking_block,
    pyproject_version,
)

ROOT = Path(__file__).resolve().parents[2]
CHANGELOG = ROOT / "CHANGELOG.md"
README = ROOT / "README.md"
ENV_EXAMPLE = ROOT / ".env.example"

# README 互指目標小節的 raw text（不追 anchor hash，見架構決策）。
README_ANCHOR_TEXT = "state 安全寫入"

# 三態語意：所有提及 TI_REQUIRE_CHOWN 的文件須一致呈現這三個值。
CHOWN_STATES = ("strict", "warn", "off")


# ---------------------------------------------------------------------------
# 純函式契約檢測器（供正向測試與反向黑樣本共用，確保兩端用同一把尺）
# ---------------------------------------------------------------------------


def _find_idx(text: str, *patterns: str) -> int:
    """回傳第一個命中 pattern 的起始 index；全部未命中回 -1。"""
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            return m.start()
    return -1


def breaking_header_idx(text: str) -> int:
    """Breaking 區塊標題起始 index；找不到回 -1。接受 ##/### 與 ⚠️ 標籤。"""
    m = re.search(r"(?im)^#{1,4}\s*.*(?:⚠️\s*)?breaking\s*change", text)
    if m:
        return m.start()
    m = re.search(r"⚠️\s*BREAKING", text)
    return m.start() if m else -1


def has_breaking_block(text: str) -> bool:
    return breaking_header_idx(text) != -1


def breaking_is_at_top(text: str) -> bool:
    """Breaking 區塊必須出現在一般變更區塊（Added/Changed/Fixed/Removed）之前。"""
    bidx = breaking_header_idx(text)
    if bidx == -1:
        return False
    others = [
        m.start()
        for m in re.finditer(
            r"(?im)^#{2,4}\s*(added|changed|fixed|removed|新增|變更|修正|移除)", text
        )
    ]
    return all(bidx < o for o in others)


def has_token(text: str, token: str) -> bool:
    return token in text


def four_elements_in_order(text: str) -> bool:
    """四要素皆存在且 index 嚴格遞增：行為 < 原因 < 遷移 < 生效版本。"""
    i_behavior = _find_idx(
        text, r"strict[^\n]{0,30}預設", r"預設[^\n]{0,30}strict", r"已改為[^\n]{0,20}strict"
    )
    i_reason = _find_idx(text, r"symlink", r"root-only", r"root\s*-?\s*only")
    i_migration = _find_idx(text, r"遷移", r"before\s*/\s*after", r"之前.{0,20}之後")
    i_version = _find_idx(
        text,
        r"自\s*0?\.?\d+\.\d+\.\d+.{0,8}起",
        r"自\s*\d+\.\d+\.\d+.{0,8}起",
        r"\d+\.\d+\.\d+\s*起.{0,6}生效",
        r"生效版本",
        r"生效",
    )
    idxs = [i_behavior, i_reason, i_migration, i_version]
    if any(i == -1 for i in idxs):
        return False
    return i_behavior < i_reason < i_migration < i_version


def has_failsafe_note(text: str) -> bool:
    """錯誤值 fail-safe 回退 strict 必須明示一行。"""
    return bool(
        re.search(r"(錯誤值|無法辨識|非法|打錯)[^\n]{0,40}(回退|fallback|退回|strict)", text)
        or re.search(r"fail-?safe[^\n]{0,40}strict", text, re.IGNORECASE)
    )


def has_warn_escape_hatch(text: str) -> bool:
    """非 root 顯式設 warn 或 off 的逃生艙說明。

    用精準 regex 匹配 TI_REQUIRE_CHOWN=warn/off，不用裸 substring——
    避免被其他文字裡的 "warning" 假命中（高工審查 #1）。
    """
    has_warn = bool(re.search(r"TI_REQUIRE_CHOWN\s*=\s*warn|CHOWN[^\n]{0,40}=\s*warn", text))
    has_off = bool(re.search(r"TI_REQUIRE_CHOWN\s*=\s*off|CHOWN[^\n]{0,40}=\s*off", text))
    has_nonroot = bool(re.search(r"非\s*root|root[^\n]{0,30}(部署|執行)", text))
    return has_warn and has_off and has_nonroot


def states_present(text: str) -> bool:
    """三態（strict/warn/off）皆在文件內出現。"""
    return all(state in text for state in CHOWN_STATES)


def has_future_enforce_timeline(text: str) -> bool:
    """是否出現『下版才 enforce／警告期後才生效』等與 strict 已成預設矛盾的未來時序。"""
    return bool(
        re.search(r"警告期(後|結束)", text)
        # 涵蓋「下版才 enforce／下一版再生效／下版才強制」等未來時序敘述。
        or re.search(r"下(一)?版.{0,8}(才|再).{0,8}(生效|強制|enforce)", text)
    )


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def changelog() -> str:
    assert CHANGELOG.exists(), (
        f"release note 檔不存在：{CHANGELOG}（驗收 #1 未達成，任務 #2/#3 尚未產出 CHANGELOG.md）"
    )
    return CHANGELOG.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 任務 #1：extractor 純函式直接 unit test（高工必修——直接呼叫被測函式本體，
#   不經過測試檔自寫的寬鬆 breaking_header_idx，確保主函式爛掉測試會紅）
# ---------------------------------------------------------------------------


def test_extract_breaking_block_real_changelog(changelog):
    """有區塊：抽出非空內容，且邊界止於下一個頂層 `## `（不洩漏到版本節）。"""
    block = extract_breaking_block(changelog)
    assert block is not None, "真實 CHANGELOG 應抽出 Breaking Changes 區塊"
    assert "TI_REQUIRE_CHOWN" in block
    assert "## [" not in block, "區塊邊界須止於下一個頂層 `## `，不得吃進版本節"


def test_extract_breaking_block_missing_returns_none():
    """缺區塊：明確回 None，非靜默空字串。"""
    assert extract_breaking_block("# Changelog\n## [0.1.0]\n- x") is None


def test_extract_breaking_block_empty_returns_none():
    """空區塊（heading 在、底下無內容）：統一回 None。"""
    text = BREAKING_HEADING + "\n\n   \n## [0.2.0]\n- y"
    assert extract_breaking_block(text) is None


def test_extract_breaking_block_at_eof():
    """EOF 邊界：Breaking 為最後一個 section（後無 `## `）仍能抽出（\\Z 覆蓋）。"""
    text = "# Changelog\n" + BREAKING_HEADING + "\n- only at eof\n"
    assert extract_breaking_block(text) == "- only at eof"


# ---------------------------------------------------------------------------
# 正向驗收（#1~#5、#7）
# ---------------------------------------------------------------------------


def test_changelog_exists():
    assert CHANGELOG.exists(), f"驗收 #1：repo 根目錄缺 release note 檔 {CHANGELOG}"


def test_version_single_source_of_truth(changelog):
    """驗收 #1：版本字串存在，且與 pyproject 單一事實來源一致。"""
    ver = pyproject_version()
    # TODO(升版門衛)：下次 bump pyproject 版本時，須同步改此處期望值與 CHANGELOG 的版本區塊。
    # 此硬寫值是刻意的 breaking-change 門衛，確保升版必伴隨 release note 更新。
    assert ver == "0.2.0", (
        f"pyproject 版本非預期 0.2.0：{ver}"
        "（若為刻意升版，請同步更新本測試 L151 期望值與 CHANGELOG 版本區塊）"
    )
    assert ver in changelog, f"CHANGELOG 未含 pyproject 版本字串 {ver!r}"


def test_breaking_block_present(changelog):
    assert has_breaking_block(changelog), "驗收 #2：缺獨立 Breaking Changes 區塊／標籤"


def test_breaking_block_at_top(changelog):
    assert breaking_is_at_top(changelog), (
        "驗收 #2：Breaking 區塊未置於一般變更區塊（Added/Changed/...）之前"
    )


@pytest.mark.parametrize("token", ["TI_REQUIRE_CHOWN", "strict", "warn", "off"])
def test_chown_tokens_present(changelog, token):
    assert has_token(changelog, token), f"驗收 #3/#4：CHANGELOG 缺必要字樣 {token!r}"


def test_four_elements_in_order(changelog):
    assert four_elements_in_order(changelog), (
        "驗收 #3：四要素（①行為②原因③遷移④生效版本）缺項或順序錯置"
    )


def test_failsafe_note_present(changelog):
    assert has_failsafe_note(changelog), "驗收 #4：未明示錯誤值 fail-safe 回退 strict"


def test_warn_escape_hatch_present(changelog):
    assert has_warn_escape_hatch(changelog), "驗收 #4：未明示非 root 設 warn/off 過渡逃生"


def test_points_to_readme_section(changelog, readme):
    """驗收 #5：CHANGELOG 指向 README「state 安全寫入」小節（raw text 一致）。"""
    assert README_ANCHOR_TEXT in changelog, (
        f"驗收 #5：CHANGELOG 未指向遷移指引位置（缺 {README_ANCHOR_TEXT!r}）"
    )
    assert README_ANCHOR_TEXT in readme, (
        f"驗收 #5：README 不含被指向的小節字串 {README_ANCHOR_TEXT!r}（死鏈）"
    )


def test_no_future_enforce_timeline(changelog):
    """驗收 #7：時序語意——strict 已是預設，禁止『下版才 enforce』等未來承諾。"""
    bidx = breaking_header_idx(changelog)
    scope = changelog[bidx:] if bidx != -1 else changelog
    assert not re.search(r"下版.{0,6}(才|再).{0,6}enforce", scope), (
        "出現與 config.py 矛盾的未來時序"
    )
    assert not re.search(r"警告期(後|結束)", scope), (
        "出現『警告期後才生效』未來時序，與 strict 已成預設矛盾"
    )


# ---------------------------------------------------------------------------
# 反向黑樣本（#6）：用同一把尺，截掉內容應 fail，證明真鑑別力
# ---------------------------------------------------------------------------


def test_black_sample_missing_breaking_block(changelog):
    """截掉 Breaking 標題後，檢測器必須翻紅（否則為假綠）。"""
    polluted = re.sub(r"(?im)^#{1,4}\s*.*breaking\s*change.*$", "## Notes", changelog)
    polluted = re.sub(r"⚠️\s*BREAKING", "NOTE", polluted)
    assert not has_breaking_block(polluted), "黑樣本失效：缺 Breaking 區塊仍被判為存在"


def test_black_sample_breaking_buried_below(changelog):
    """把一般區塊挪到 Breaking 之前，置頂檢測必須翻紅。"""
    buried = "## [0.2.0]\n### Added\n- something\n\n" + changelog
    assert not breaking_is_at_top(buried), "黑樣本失效：Breaking 被埋在下方仍判為置頂"


def test_black_sample_missing_token(changelog):
    """移除 TI_REQUIRE_CHOWN 字樣，token 檢測必須翻紅。"""
    polluted = changelog.replace("TI_REQUIRE_CHOWN", "SOME_OTHER_VAR")
    assert not has_token(polluted, "TI_REQUIRE_CHOWN"), "黑樣本失效：缺字樣仍判為存在"


def test_black_sample_missing_version(changelog):
    """移除版本字串，版本檢測必須翻紅。"""
    ver = pyproject_version()
    polluted = changelog.replace(ver, "")
    assert ver not in polluted, "黑樣本失效：版本字串移除後仍存在"


def test_black_sample_elements_out_of_order(changelog):
    """把『生效版本』整段挪到最前，順序檢測必須翻紅。"""
    scrambled = "（自 0.2.0 起生效）\n" + changelog
    # 生效版本被前置後，i_version 變成最小，順序遞增不再成立
    assert not four_elements_in_order(scrambled), "黑樣本失效：四要素順序錯置仍判為合格"


def test_black_sample_missing_warn_escape_hatch(changelog):
    """移除逃生艙說明（warn/off 改回 strict），warn/off 檢測必須翻紅。"""
    polluted = re.sub(r"TI_REQUIRE_CHOWN\s*=\s*(warn|off)", "TI_REQUIRE_CHOWN=strict", changelog)
    polluted = re.sub(r"(?<!\w)(warn|off)(?!\w)", "strict", polluted)
    assert not has_warn_escape_hatch(polluted), "黑樣本失效：缺逃生艙說明仍判為存在"


def test_black_sample_missing_failsafe(changelog):
    """抽掉 fail-safe 字樣，檢測必須翻紅。"""
    polluted = re.sub(
        r"(錯誤值|無法辨識|非法|打錯|fail-?safe)", "X", changelog, flags=re.IGNORECASE
    )
    assert not has_failsafe_note(polluted), "黑樣本失效：缺 fail-safe 說明仍判為存在"


# ---------------------------------------------------------------------------
# 任務 #5（驗收 #7）：跨檔一致性——CHANGELOG／README／.env.example
#   三態語意、warn/off 指引、版本字串無矛盾，無未來時序。
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def readme() -> str:
    assert README.exists(), f"驗收 #7：缺 README.md {README}"
    return README.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def env_example() -> str:
    assert ENV_EXAMPLE.exists(), f"驗收 #7：缺 .env.example {ENV_EXAMPLE}"
    return ENV_EXAMPLE.read_text(encoding="utf-8")


def test_readme_mentions_chown(readme):
    assert "TI_REQUIRE_CHOWN" in readme, "驗收 #7：README 未提及 TI_REQUIRE_CHOWN"


def test_env_example_mentions_chown(env_example):
    assert "TI_REQUIRE_CHOWN" in env_example, "驗收 #7：.env.example 未提及 TI_REQUIRE_CHOWN"


def test_three_states_consistent_across_docs(changelog, readme, env_example):
    """三態（strict/warn/off）在三份文件中一致呈現，無任一檔漏列。"""
    for name, text in (("CHANGELOG", changelog), ("README", readme), (".env.example", env_example)):
        missing = [s for s in CHOWN_STATES if s not in text]
        assert not missing, f"驗收 #7：{name} 三態不一致，缺 {missing}"


def test_strict_is_default_across_docs(changelog, readme, env_example):
    """三份文件須一致宣告 strict 為預設值，不得有某檔說別的預設。"""
    for name, text in (("CHANGELOG", changelog), ("README", readme), (".env.example", env_example)):
        assert re.search(r"strict[^\n]{0,12}預設|預設[^\n]{0,12}strict", text), (
            f"驗收 #7：{name} 未一致宣告 strict 為預設"
        )


def test_failsafe_consistent_across_docs(readme, env_example):
    """README／.env.example 也須與 CHANGELOG 一致載明錯誤值 fail-safe 回退 strict。"""
    assert has_failsafe_note(readme), "驗收 #7：README 缺 fail-safe 回退 strict 說明"
    assert has_failsafe_note(env_example), "驗收 #7：.env.example 缺 fail-safe 回退 strict 說明"


def test_no_future_enforce_timeline_across_docs(changelog, readme, env_example):
    """三份文件皆不得出現『下版才 enforce／警告期後』等與 strict 已成預設矛盾的未來時序。

    註（D2 掃描範圍差異）：本測掃全文，與 test_no_future_enforce_timeline 限縮 Breaking 節不同——
    後者守『Breaking 條目內』語意，本測守『整份文件不得有矛盾路線圖』。
    若未來 CHANGELOG 要加合法的「下版路線圖」段落，需改為節內限縮或加白名單。
    """
    for name, text in (("CHANGELOG", changelog), ("README", readme), (".env.example", env_example)):
        assert not has_future_enforce_timeline(text), f"驗收 #7：{name} 出現未來時序矛盾"


# 註：版本單一事實來源由 test_version_single_source_of_truth（pyproject↔CHANGELOG）守住；
#     README／.env.example 刻意不載 release 版本字串，無可矛盾，故不在此重複掃描
#     （全域 X.Y.Z 掃描會誤判 127.0.0.1／10.0.0.0 等 IP，屬脆弱設計，故不採）。


# --- 反向黑樣本：證明跨檔檢測器有真鑑別力 ---


def test_black_sample_state_dropped_in_doc(env_example):
    """從文件副本抽掉某一態，三態一致檢測必須翻紅。"""
    assert "warn" in env_example, "黑樣本前提失效：env_example 本無 warn，replace 為空操作"
    polluted = env_example.replace("warn", "XXXX")
    assert not states_present(polluted), "黑樣本失效：缺 warn 態仍判為三態齊全"


def test_black_sample_future_timeline_detected(changelog):
    """注入未來時序語句，矛盾檢測必須翻紅。"""
    polluted = changelog + "\n下版才 enforce strict。\n"
    assert has_future_enforce_timeline(polluted), "黑樣本失效：未來時序未被偵測"
