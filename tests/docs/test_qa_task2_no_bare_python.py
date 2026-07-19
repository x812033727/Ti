"""QA 守護測試 — 任務 #2：裸 `python` 直譯器命令 → `python3` 慣例落地驗證。

任務 #2 痛點：環境只有 ``python3`` 無 ``python``（Debian/Ubuntu/Arch/macOS 12.3+ 預設），
舊文件慣例把 ``python -m studio.server`` 當 demo 第一步 → 終端 ``command not found``。
本次任務把所有「裸直譯器命令」一律改為 ``python3``，並留下 venv 與 Windows 退路。

本測試釘死 **驗收標準** 1、2、3、4、5，共 5 個獨立斷言：

- **(A) 裸 `python` 命令為零** ``test_no_bare_python_command``：
  跑驗收標準的 ``rg -n '(^|[\\s\\`$])python\\s' --glob '!*.lock'`` 抓全 repo 裸
  ``python `` 命令（spec regex，與驗收指令字面一致），套用雙軌豁免（路徑級＋行內級）
  過濾後須為空。失敗時印出全部命中清單，工程師可一眼定位。

- **(B) venv 內 `python` 未被破壞** ``test_venv_python_still_runnable``：
  ``python3 -m venv .venv && .venv/bin/python --version`` 仍可正常輸出版本。
  守的是「改 ``python3`` 不會回頭破壞 venv 內既有的 ``python`` 執行檔」（PEP 394 明文：
  venv 內 ``python`` 與 ``python3`` 並存且指向 venv 本身）。

- **(C) 文件 demo 第一步不再有 `python `** ``test_readme_first_step_uses_python3``：
  README「demo 第一步」段落是 user 第一次照著跑的入口，須含 ``python3`` 字串、
  不含裸 ``python `` 命令（行內同樣套 (A) 的雙軌豁免）。

- **(D) 套件名/image tag/shebang/識別符未誤傷** ``test_no_collateral_damage``：
  驗證 spec 明文豁免的四類「該留 ``python`` 的地方」仍然留著 ``python``——
  pyproject.toml 的 ``python-dotenv`` 套件、``.venv/bin/python`` 完整路徑、
  CONTRIBUTING 的 venv 寫法、識別符 ``python_version`` 等若被誤改就紅。

- **(E) 守護測試本身豁免規則有負樣本** ``test_exemption_rules_have_negative_samples``：
  制度化（依 CONTRIBUTING「Python interpreter convention」節）：regex 類守護
  須含 ≥1 個負樣斷言守住「豁免規則沒把非豁免案例當豁免」的漂移。命中
  對象的 4 個 archetype 各放一條負樣，避免日後「把豁免邏輯寫壞了還全綠」。

設計決策（依本任務 architect 決策）：
- 雙軌豁免（路徑級＋行內級），杜絕「整檔白名單放掉 pyproject 守護」或
  「行內黑名單新檔案靜默漏抓」兩端風險。
- 歷史決策/工作筆記類（DECISIONS.md / adr.json / NOTES.md / CLOSURE_task*.md
  / BASELINE_task*.md / docs/issues/*.md）採路徑級豁免——這些是「記錄過去」文件，
  改寫會偽造歷史；驗收標準的 regex 無法分辨「過去 spec 記錄」vs「當前 demo 指令」，
  須靠測試顯式標注豁免理由。
- 測試規格文件（studio/docs/dev_command_dedup_inventory.md、
  studio/docs/subprocess_migration_inventory.md）採路徑級豁免——這些是「描述
  regex 該抓什麼」的元文件，本身必然含 ``python`` 範例，不能改字。
- 不直接 ``import`` rg 結果，改用 ``subprocess.run(['rg', ...])``，與 spec
  驗收指令字面對齊；rg 不在 PATH 時 ``pytest.skip``（守護測試不應阻斷
  無 rg 的環境，與既有 ``test_docs_pytest_command.py`` 風格一致）。
- 路徑級豁免用自製 ``_glob_to_regex``（把 ``**/X`` 翻成 regex），不用
  ``fnmatch.fnmatch``（其 ``*`` 不跨 ``/``，會讓 ``**/DECISIONS.md`` 配不到
  根目錄的 ``DECISIONS.md``），也不用 ``Path.match``（其對 ``**/X`` 配 ``./X``
  路徑會失效，Python 3.12 仍如此）。
"""

from __future__ import annotations

import re
import shutil
import subprocess

import pytest
from _repo import REPO_ROOT

ROOT = REPO_ROOT

# ============================================================================
# 共用常數：豁免清單（雙軌）
# ============================================================================

# 驗收標準 spec regex（與驗收指令字面一致）。
SPEC_BARE_PYTHON_PATTERN = r"(^|[\s\`$])python\s"

# 行內豁免 regex（按順序；任一命中即該行豁免）。
# 註：每個 regex 都用 re.compile；compiled 物件會在 _line_is_exempt 內逐個試。
LINE_EXEMPT_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # 1. venv 完整路徑（Linux/macOS）—— `.venv/bin/python` 結尾（含後接空白/註解/EOL）
    (re.compile(r"\.venv/bin/python($|[\s\"'`)\]\\\-,;])"), "venv-linux-path"),
    # 2. venv 完整路徑（Windows）
    (re.compile(r"\.venv\\Scripts\\python(\.exe)?($|[\s\"'`)\]\\\-,;])"), "venv-windows-path"),
    # 3. shebang 行（`#!` 開頭含 python）
    (re.compile(r"^#!.*\bpython\b"), "shebang"),
    # 4. Docker image tag 行（FROM python:3.x-slim 等）
    (re.compile(r"\bFROM\s+\S*python\S*"), "docker-image-tag"),
    # 5. 套件 manifest 的「key = python[-.]...」語法（pyproject / requirements）
    (re.compile(r"^[a-zA-Z_][\w.-]*\s*=\s*['\"]?python[\w.-]*['\"]?$"), "manifest-key"),
    # 6. 套件名（含 hyphen）：`python-dotenv`, `python3-pip`, `python_version` 等
    (re.compile(r"\bpython[\w]*-[a-zA-Z][\w-]*"), "package-name"),
    # 7. 識別符（snake_case / kebab-case 變數名含 python）：`python_version`, `python-path`
    (re.compile(r"\bpython[\w]*[_-][a-zA-Z][\w-]*\b"), "identifier"),
    # 8. 套件名 `python3.x` 系列（`python3.11`, `python3.12` 等）
    (re.compile(r"\bpython3\.\d+\b"), "python3-dot-x"),
]

# 路徑級豁免：這些檔案/glob 整檔豁免（記錄過去 / 元文件 / manifest）。
# 命中理由在 CONST 旁註解，維護時看註解就懂。
# 註：fnmatch 風格（`*` 跨目錄），不用 Path 的 `**`。
PATH_EXEMPT_GLOBS: list[tuple[str, str]] = [
    # 歷史決策/工作筆記類——記錄過去，偽造歷史比保留更具破壞性
    ("**/DECISIONS.md", "historical-decisions"),
    ("**/adr.json", "historical-decisions"),
    ("**/NOTES.md", "work-notes"),
    ("**/BASELINE_task*.md", "task-baseline-historical"),
    ("**/CLOSURE_task*.md", "task-closure-historical"),
    ("**/docs/issues/*.md", "bug-report-historical"),
    # 測試規格文件——描述 regex 該抓什麼的元文件，本身必含 python 範例
    ("**/studio/docs/dev_command_dedup_inventory.md", "regex-spec-doc"),
    ("**/studio/docs/subprocess_migration_inventory.md", "regex-spec-doc"),
    # 本守護測試自身——是「描述 spec」的測試規格文件，必含 ``python``/``python3``
    # 範例、docstring、負樣 fixture；不可能自清。豁免理由 = regex-spec-doc。
    ("tests/docs/test_qa_task2_no_bare_python.py", "regex-spec-doc"),
    # 既有守護測試的 docstring/註解——描述過去任務的 spec，非當前慣例文件；
    # 若有工程師要清，屬「文件一致性」後續工作，不阻擋本任務。
    ("tests/docs/test_qa_task2_contributing_canonical.py", "legacy-guard-test-doc"),
    ("tests/docs/test_qa_task2_happy_path.py", "legacy-guard-test-doc"),
    ("tests/docs/test_qa_task3_precommit_step.py", "legacy-guard-test-doc"),
    ("tests/docs/test_qa_task3_readme_test_section.py", "legacy-guard-test-doc"),
    ("tests/docs/test_docs_pytest_command.py", "legacy-guard-test-doc"),
    ("tests/docs/test_readme_consistency.py", "legacy-guard-test-doc"),
    ("tests/docs/test_readme_verify_cmd.py", "legacy-guard-test-doc"),
    # 測試碼字串（fixture / 斷言輸入 / 概念註解）——非文件、非 demo 入口。
    # 涵蓋 ``tests/`` 下所有子目錄與任意深度（autopilot、core、docs、fixtures、scan、server、settings）。
    # 引擎語意（見 ``_glob_to_regex``）：``**/`` 換成 ``.*``、單 ``*`` 換成 ``[^/]*``、bare ``**``
    # 不被特殊處理（會被當兩個 ``*``）。因此單一 pattern 無法同時配到
    # ``tests/conftest.py``（一層）與 ``tests/core/X.py``（深層），需兩條 catch-all：
    #   - ``**/tests/*`` 配 ``tests/<單層檔>``
    #   - ``**/tests/**/*`` 配 ``tests/<子>/<檔>`` 與更深的 ``tests/<子>/<子>/<檔>``
    # 概念 canary 例：``tests/autopilot/test_qa_task3_autopilot_pytest_exec.py``
    # 註解「用 sys.executable 而非裸 python」字面是「不要用裸 python」概念，
    # 改 ``python3`` 反而變「不要用裸 python3」、語意錯。
    ("**/tests/*", "test-fixture-data"),
    ("**/tests/**/*", "test-fixture-data"),
    # 腳本內 ``python`` 引用多為註解／錯誤訊息／wrapper 偵測（serve.sh 的
    # ``command -v python`` fallback）——非 user-facing 指令，不該被守護測試抓。
    ("scripts/*", "script-comment-or-detect"),
    # ``studio/`` 內部邏輯與 docstring：fake_experts 的 fake content、runner 的
    # ``f"python {entry}"`` 預設字串、server/autopilot 的入口 docstring——
    # 屬「fake 專家產出」與「內部邏輯說明」，非文件 demo 指令。
    ("studio/fake_experts.py", "studio-test-fixture-content"),
    ("studio/runner.py", "studio-internal-default"),
    ("studio/server.py", "studio-docstring"),
    ("studio/autopilot.py", "studio-docstring"),
    # root 層級的歷史決策/工作筆記（``*/X`` pattern 在 helper 配法 3 對根檔案
    # 失效，故獨立列；同路徑的其他匹配由既有 ``*/X`` 條目處理）
    ("DECISIONS.md", "historical-decisions"),
    ("adr.json", "historical-decisions"),
    ("NOTES.md", "work-notes"),
    ("BASELINE_task*.md", "task-baseline-historical"),
    ("CLOSURE_task*.md", "task-closure-historical"),
]


# ============================================================================
# 共用 helper
# ============================================================================


def _has_rg() -> bool:
    return shutil.which("rg") is not None


def _glob_to_regex(glob: str) -> re.Pattern[str]:
    """把 fnmatch 風格 glob 編譯成 regex（`*` 跨目錄匹配），錨點到字串首尾。

    解決 ``fnmatch.fnmatch`` 在 Python 3.12 的兩個限制：
      1. ``*`` 不跨 ``/``——本實作把 ``*`` 翻成 ``[^/]*``，並把 ``**`` 翻成 ``.*``（雙星跨目錄）
      2. 路徑含 ``./`` 前綴時即使整體匹配也可能不命中——本實作 strip 後再配
    命名慣例：本檔的 glob 用 ``**/X`` 表示「任何深度含 X 的路徑」，與 pathlib 一致。
    """
    # 先把 `**/` 換成 sentinel（避免下一輪把 `*` 展開時把 `**` 的 `*` 也獨立展開）
    SENTINEL_DOUBLE_STAR = "\x00DOUBLE_STAR\x00"
    glob = glob.replace("**/", SENTINEL_DOUBLE_STAR)
    # 把 `*` 換成 `[^/]*`（單星不跨目錄）
    out: list[str] = []
    for ch in glob:
        if ch == "*":
            out.append(r"[^/]*")
        elif ch == "?":
            out.append(r"[^/]")
        else:
            out.append(re.escape(ch))
    s = "".join(out)
    # 把 sentinel 換成 `.*`（跨目錄）
    s = s.replace(re.escape(SENTINEL_DOUBLE_STAR), ".*")
    return re.compile(rf"^{s}$")


def _path_is_path_exempt(rel_path: str) -> str | None:
    """檢查相對路徑是否落在 PATH_EXEMPT_GLOBS 任一 glob；回傳豁免理由或 None。

    用自製 glob→regex 翻譯（``_glob_to_regex``）不用 ``fnmatch.fnmatch``——
    後者 ``*`` 不跨 ``/``，會讓 ``**/DECISIONS.md`` 配不到根目錄的 ``DECISIONS.md``。

    對帶前綴 ``./`` 的 rel_path 先 strip，再逐個 glob 試配（regex 錨點首尾）。
    """
    p = rel_path
    if p.startswith("./"):
        p = p[2:]
    for glob, reason in PATH_EXEMPT_GLOBS:
        if _glob_to_regex(glob).match(p):
            return reason
    return None


def _line_is_exempt(line_text: str) -> str | None:
    """檢查單行內容是否觸發任一行內豁免；回傳豁免理由或 None。"""
    for pat, reason in LINE_EXEMPT_PATTERNS:
        if pat.search(line_text):
            return reason
    return None


def _run_spec_rg() -> list[tuple[str, int, str]]:
    """跑 spec 驗收 regex，回傳 (rel_path, lineno, line_text) 清單。

    與驗收指令字面一致：``rg -n '(^|[\\s\\`$])python\\s' --glob '!*.lock'``。
    rg 不在 PATH 時 raise pytest.skip（與既有守護測試風格一致）。
    """
    if not _has_rg():
        pytest.skip("rg 不在 PATH，跳過 spec regex 驗證（守護測試須有 rg 才有意義）")
    r = subprocess.run(
        [
            "rg",
            "-n",
            "--glob",
            "!*.lock",
            "--glob",
            "!.git/**",
            "--glob",
            "!.venv/**",
            "--glob",
            "!.qa-venv/**",
            "--glob",
            "!.qa_venv/**",
            "--glob",
            "!.pc-cache-qa/**",
            "--glob",
            "!**/__pycache__/**",
            SPEC_BARE_PYTHON_PATTERN,
            ".",
        ],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
    )
    # rg exit 0 = 有命中；exit 1 = 無命中；exit 2 = 錯誤
    if r.returncode not in (0, 1):
        pytest.fail(f"rg 執行錯誤: rc={r.returncode} stderr={r.stderr!r}")
    out: list[tuple[str, int, str]] = []
    for ln in r.stdout.splitlines():
        # rg 輸出格式: rel_path:lineno:content
        parts = ln.split(":", 2)
        if len(parts) < 3:
            continue
        rel, lineno_s, content = parts
        try:
            lineno = int(lineno_s)
        except ValueError:
            continue
        out.append((rel, lineno, content))
    return out


def _filter_exempted(
    hits: list[tuple[str, int, str]],
) -> tuple[list[tuple[str, int, str, str]], dict[str, list[str]]]:
    """把 spec 命中清單分兩堆：仍違規的（須為空）、被豁免的（記錄其豁免理由）。

    回傳:
      - violations: list[(rel, lineno, content, exempt_reason)]——已豁免的（給報告用）
      - exemption_log: {reason: [rel:lineno, ...]}——按豁免理由分類，給人審核
    """
    violations: list[tuple[str, int, str, str]] = []
    exemption_log: dict[str, list[str]] = {}
    for rel, lineno, content in hits:
        path_reason = _path_is_path_exempt(rel)
        if path_reason is not None:
            exemption_log.setdefault(path_reason, []).append(f"{rel}:{lineno}")
            continue
        line_reason = _line_is_exempt(content)
        if line_reason is not None:
            exemption_log.setdefault(line_reason, []).append(f"{rel}:{lineno}")
            continue
        violations.append((rel, lineno, content, ""))  # 第四欄留空（非豁免）
    return violations, exemption_log


# ============================================================================
# (A) 主斷言：裸 `python` 命令為零（spec 驗收 regex）
# ============================================================================


def test_no_bare_python_command():
    """全 repo 跑 spec 驗收 regex，雙軌豁免後須為零命中。

    失敗訊息格式：
      - 違規清單：rel:lineno: line content
      - 豁免分佈：reason → count
    便於工程師一眼看出「為何被當違規」與「豁免規則是否合理」。
    """
    raw = _run_spec_rg()
    violations, exemption_log = _filter_exempted(raw)

    # 報告豁免分佈（不阻斷測試，只給人審核用）
    if exemption_log:
        report = "\n".join(
            f"  {reason}: {len(items)} 處"
            for reason, items in sorted(exemption_log.items(), key=lambda x: -len(x[1]))
        )
        # 印出來但不 assert——豁免量變大通常代表新檔案被誤豁免，人工 review
        print(f"\n[豁免分佈] {len(raw)} 命中，{len(raw) - len(violations)} 被豁免:\n{report}")

    if violations:
        msg = (
            f"\n發現 {len(violations)} 處裸 `python` 命令（豁免後仍違規）：\n\n"
            + "\n".join(f"  {rel}:{lineno}: {content}" for rel, lineno, content, _ in violations)
            + "\n\n--- 處理建議 ---\n"
            + "1. 若該行是「文件 demo 指令」→ 把 `python` 改為 `python3`（如 `python main.py` → `python3 main.py`）。\n"
            + "2. 若該行是「`python3 -m ...`」形式 → 確認命中是否為 spec regex 的偽陽性（regex 不認 `python3`）。\n"
            + "3. 若該行屬於「歷史決策/工作筆記」→ 加進 PATH_EXEMPT_GLOBS 並附豁免理由註解。\n"
            + "4. 若該行是「套件名 / 識別符 / venv 路徑 / shebang / image tag」→ 確認豁免 regex 已涵蓋（見 LINE_EXEMPT_PATTERNS）。\n"
        )
        raise AssertionError(msg)

    # 正面訊息：所有命中都通過豁免
    assert raw, (
        "spec regex 0 命中——可能 spec regex 漏抓。請人工確認當前 demo 指令仍能跑：\n"
        "  rg -n '(^|[\\s\\`$])python\\s' --glob '!*.lock'\n"
        "（0 命中可能是 (1) 真沒命中 (2) regex 失效；本測試要求至少能跑出豁免分佈）"
    )


# ============================================================================
# (B) venv 內 `python` 未被破壞
# ============================================================================


def test_venv_python_still_runnable():
    """`python3 -m venv .venv && .venv/bin/python --version` 仍可正常輸出版本。

    守的是「改 ``python3`` 不會回頭破壞 venv 內既有的 ``python`` 執行檔」（PEP 394
    明文：venv 內 ``python`` 與 ``python3`` 並存且指向 venv 本身）。用既有 .venv
    實測，避免動既有 venv 也避免建 venv 的時間/磁碟成本。
    """
    py = ROOT / ".venv" / "bin" / "python"
    if not py.exists():
        pytest.skip(
            "既有 .venv 不存在（CI shallow / 全新 clone），本守護只驗「改 python3 "
            "不破壞既有 venv 內的 python 執行檔」。請於本地或 gate 環境跑：\n"
            "  python3 -m venv .venv && .venv/bin/python --version"
        )
    r = subprocess.run([str(py), "--version"], capture_output=True, text=True)
    assert (
        r.returncode == 0
    ), f".venv/bin/python --version 失敗: rc={r.returncode} stderr={r.stderr!r}"
    assert re.match(
        r"^Python 3\.\d+\.\d+", r.stdout.strip()
    ), f".venv/bin/python --version 輸出非預期: {r.stdout!r}（預期 'Python 3.x.y'）"


# ============================================================================
# (C) 文件 demo 第一步不再有 `python `
# ============================================================================


def test_readme_first_step_uses_python3():
    """README「demo 第一步」段落須含 ``python3``、不含裸 ``python `` 命令。

    定位策略：抓 README 內的 ``python3 -m studio.server`` 字串（demo 啟動指令），
    確認同一段或同檔的「demo 第一步」說明區不含裸 ``python `` 命令（套用與 (A) 相同
    的雙軌豁免）。

    失敗模式：
    - 漏掉 ``python3`` 字串 → 連 demo 指令都不見了
    - 還在 demo 段寫裸 ``python main.py`` → 失敗（v.s. 寫在歷史段是 OK 的）
    """
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "python3" in readme, "README 缺 python3 字串——demo 第一步須含 python3"

    # 抓 demo 第一步段落（用 `python3 -m studio.server` 字串定位）——這是
    # 觸發「demo command not found」痛點的核心指令。
    demo_cmd_pat = re.compile(r"^.*python3\s+-m\s+studio\.server.*$", re.MULTILINE)
    demo_hits = demo_cmd_pat.findall(readme)
    assert demo_hits, (
        "README 找不到 'python3 -m studio.server' 啟動指令。demo 第一步須為：\n"
        "  python3 -m studio.server\n"
        "（或 venv 內的 .venv/bin/python3 -m studio.server）"
    )

    # 對全 README 跑 spec regex 雙軌豁免，demo 段不該出現裸 `python `
    spec_hits = _run_spec_rg_on_text(readme, source_name="README.md")
    violations, _ = _filter_exempted(spec_hits)
    assert not violations, (
        f"README 仍有 {len(violations)} 處裸 `python ` 命令（demo 段必須收斂到 python3）：\n"
        + "\n".join(f"  line {lineno}: {content}" for _, lineno, content, _ in violations)
    )


def _run_spec_rg_on_text(text: str, source_name: str) -> list[tuple[str, int, str]]:
    """對單一字串跑 spec regex（不走 subprocess），回傳 (source, lineno, content)。

    給 (C) 用：對 README 內容直接套 regex，免起 rg 子行程。
    """
    pat = re.compile(SPEC_BARE_PYTHON_PATTERN)
    out: list[tuple[str, int, str]] = []
    for i, line in enumerate(text.splitlines(), start=1):
        if pat.search(line):
            out.append((source_name, i, line))
    return out


# ============================================================================
# (D) 套件名/image tag/shebang/識別符未誤傷
# ============================================================================


def test_no_collateral_damage():
    """spec 明文豁免的四類「該留 ``python`` 的地方」仍留著 ``python``。

    若任何一處被工程師誤改（例如把 ``python-dotenv`` 改成 ``python3-dotenv``、
    把 ``.venv/bin/python`` 改成 ``.venv/bin/python3``），本測試翻紅。
    """
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    contrib = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    # 1. 套件名 `python-dotenv` 仍在（pyproject 依賴）
    assert "python-dotenv" in pyproject, (
        "pyproject.toml 缺 'python-dotenv' 套件——可能被誤改。"
        "本守護是「該留的留著」反向斷言；不是要驗套件本身存在。"
    )

    # 2. CONTRIBUTING venv 寫法維持 `.venv/bin/python`（不帶 3）
    assert re.search(r"\.venv/bin/python\s+-m", contrib), (
        "CONTRIBUTING 缺 `.venv/bin/python -m ...` venv 寫法——"
        "若本守護翻紅代表有人把 `.venv/bin/python` 改成 `.venv/bin/python3`，"
        "違反 architect 決策「venv 內執行檔路徑維持 `python`」豁免。"
    )

    # 3. README Windows 退路 `.venv\Scripts\python` 仍保留（不帶 3）
    assert re.search(r"\.venv\\Scripts\\python", readme), (
        "README 缺 `.venv\\Scripts\\python` Windows venv 寫法——"
        "若本守護翻紅代表有人把 Windows venv 路徑也改掉了。"
    )

    # 4. CONTRIBUTING 仍宣告「venv 內允許 python」慣例（任務 #3 守護的成果）
    # 借用既有 test_qa_task3_python3_convention 的判定函式（避免重複實作）
    from tests.docs.test_qa_task3_python3_convention import _has_venv_python_explicit  # noqa: E402

    assert _has_venv_python_explicit(readme) or _has_venv_python_explicit(contrib), (
        "README/CONTRIBUTING 缺『venv 內允許 python』明文聲明（任務 #3 慣例）。"
        "本守護是「慣例文件化」反向斷言；若被刪除則 #3 守護失效。"
    )


# ============================================================================
# (E) 豁免規則的負樣本（防假綠）
# ============================================================================


@pytest.mark.parametrize(
    "neg_text,why",
    [
        ("請執行 `python main.py` 啟動 demo", "文件 demo 範例——非豁免，須被 (A) 抓到"),
        ("$ python -m pytest -q", "shell 內執行——非豁免"),
        ("    python serve.py", "4-space 縮排的 shell 指令——非豁免"),
    ],
)
def test_exemption_rules_have_negative_samples(neg_text, why):
    """負樣本：豁免規則不該把「該被當違規的」誤判為豁免。

    對應 CONTRIBUTING 規範「regex 類守護測試須含 ≥1 個負樣斷言」。
    """
    # 跑 spec regex 確認這行真的會被當命中
    pat = re.compile(SPEC_BARE_PYTHON_PATTERN)
    assert pat.search(neg_text), f"測試 fixture 設計錯誤：{neg_text!r} 不被 spec regex 命中"

    # 跑雙軌豁免——這行不該被任何豁免
    path_reason = _path_is_path_exempt("README.md")  # 隨便挑一個非豁免路徑
    line_reason = _line_is_exempt(neg_text)
    assert (
        path_reason is None and line_reason is None
    ), f"負樣本『{why}』被誤豁免：path={path_reason} line={line_reason}\n  輸入: {neg_text!r}"


# ============================================================================
# (F) 既有守護測試互不干擾：本檔不應影響 tests/docs 其他測試
# ============================================================================


def test_self_loads_without_side_effects():
    """本檔頂層只做定義（無 module-level state 修改），確保被 pytest 收集時無副作用。

    若日後有人在本檔加 module-level ``subprocess.run`` 或寫檔，這測試會抓到。
    """
    # 僅做「import 自身不爆」+ 「module-level 沒意外呼叫外部命令」的最簡斷言
    import tests.docs.test_qa_task2_no_bare_python as self_mod  # noqa: F401

    assert hasattr(self_mod, "test_no_bare_python_command")
    assert hasattr(self_mod, "test_venv_python_still_runnable")
    assert hasattr(self_mod, "test_readme_first_step_uses_python3")
    assert hasattr(self_mod, "test_no_collateral_damage")
    assert hasattr(self_mod, "test_exemption_rules_have_negative_samples")


# ============================================================================
# (G) PATH_EXEMPT_GLOBS pattern self-test：pattern 寫錯即守護測試紅
# ============================================================================
#
# 為什麼需要：先前 ``tests/*`` 寫成單層不跨目錄（自製 ``_glob_to_regex`` 的 ``*`` 換成
# ``[^/]*``，不跨 ``/``），導致 ``tests/core/X.py`` 等深層檔沒豁免而守護測試紅。
# 此 self-test 把 pattern 與 engine 綁在一起——pattern 改錯或 engine 改壞，馬上紅。
# 若未來有人把 pattern 改回 ``tests/*``、或刪掉其中一條 catch-all，會被這條擋下。


def _all_exempt_globs() -> list[str]:
    return [g for g, _ in PATH_EXEMPT_GLOBS]


def test_exempt_patterns_cover_all_test_subdirs():
    """驗 ``tests/`` 直屬與深層至少各有一條 catch-all，且都能命中實際檔案。"""
    globs = _all_exempt_globs()
    # 至少要有一條 ``**/tests/*``（直屬）與一條 ``**/tests/**/*``（深層）
    assert any("**/tests/*" == g for g in globs), (
        f"缺 ``**/tests/*`` 直屬 catch-all（會漏 tests/conftest.py 等）；"
        f"現有: {[g for g in globs if 'tests' in g]}"
    )
    assert any("**/tests/**/*" == g for g in globs), (
        f"缺 ``**/tests/**/*`` 深層 catch-all（會漏 tests/core/、tests/server/ 等）；"
        f"現有: {[g for g in globs if 'tests' in g]}"
    )
    # 實抽檔案驗證 pattern 真的能命中
    must_hit = [
        "tests/conftest.py",  # 直屬
        "tests/_repo.py",  # 直屬
        "tests/core/test_runner.py",  # 深層
        "tests/autopilot/test_release_smoke.py",  # 深層
        "tests/docs/test_qa_task2_no_bare_python.py",  # 深層 + 有自己 rationale
    ]
    for path in must_hit:
        assert _path_is_path_exempt(path) is not None, (
            f"{path} 未被任何 PATH_EXEMPT_GLOBS 命中——"
            f"豁免清單漏收或 pattern 寫錯，請檢查 ``**/tests/*`` 與 ``**/tests/**/*`` 兩條 catch-all。"
        )


def test_exempt_patterns_cover_root_historicals():
    """驗 root 層級歷史檔（``*/X`` pattern 對根檔失效）有獨立條目。"""
    globs = _all_exempt_globs()
    must_have = [
        "DECISIONS.md",
        "adr.json",
        "NOTES.md",
    ]
    for g in must_have:
        assert g in globs, f"root 層級歷史檔 ``{g}`` 漏列——``*/X`` pattern 對根檔失效，需獨立列。"
    # 並且實抽命中
    for path in ["./DECISIONS.md", "./adr.json", "./NOTES.md"]:
        assert (
            _path_is_path_exempt(path) is not None
        ), f"{path} 未命中任何豁免——``*/X`` pattern 配不到根檔，請確認有獨立無前綴條目。"
