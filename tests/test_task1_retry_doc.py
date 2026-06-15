"""QA 驗收：ARCHITECTURE.md「LLM 韌性中介層（retry 子系統）」小節。

純文件任務，逐項對驗收標準；並交叉反查文中錨點與現碼一致。
執行：pytest tests/test_task1_retry_doc.py -v
"""
import pathlib
import re
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[1]
ARCH = ROOT / "ARCHITECTURE.md"


def read(p):
    return (ROOT / p).read_text(encoding="utf-8")


def arch_section():
    """擷取新小節（## LLM 韌性中介層 ... 至下一個 H2）。"""
    txt = ARCH.read_text(encoding="utf-8")
    m = re.search(r"^## LLM 韌性中介層（retry 子系統）\n(.*?)(?=^## )", txt, re.S | re.M)
    assert m, "找不到新小節 '## LLM 韌性中介層（retry 子系統）'（H2 層級）"
    return m.group(1)


# ── 驗收標準 1：新節存在、'唯一' 字樣、三者角色點名 ──
def test_section_exists_and_unique_factory():
    sec = arch_section()
    assert "make_retry_config" in sec
    # 必須用「唯一」字樣描述工廠入口
    assert re.search(r"make_retry_config.{0,40}唯一|唯一.{0,40}make_retry_config", sec), \
        "未以『唯一』字樣描述 make_retry_config 工廠入口"
    # 三者角色都要點名
    assert "RetryConfig" in sec, "未點名 RetryConfig"
    assert "run_with_retries" in sec, "未點名 run_with_retries"
    assert "執行骨幹" in sec, "未描述 run_with_retries 為執行骨幹"


# ── 驗收標準 2：接入契約三步 + 禁第二層 retry ──
def test_provider_contract_three_steps_and_no_second_retry():
    sec = arch_section()
    assert "接入契約" in sec, "缺『新 provider 接入契約』段"
    # 三步驟錨點
    assert "make_retry_config" in sec
    assert "as_kwargs" in sec, "契約未提 as_kwargs() 平鋪步驟"
    assert "run_with_retries" in sec
    # 三步驟需有序：make_retry_config 出現在 as_kwargs 之前、as_kwargs 在 run_with_retries 之前
    contract = sec[sec.index("接入契約"):]
    i_make = contract.index("make_retry_config")
    i_kw = contract.index("as_kwargs")
    i_run = contract.index("run_with_retries")
    assert i_make < i_kw < i_run, "接入契約三步順序不符（取 cfg → as_kwargs → run_with_retries）"
    # 禁第二層 retry 明文
    assert re.search(r"禁止.{0,20}retry|禁止.{0,20}第二層", sec), "未明文『禁止 provider 層第二層 retry』"
    assert "max_retries=0" in sec, "未標明 SDK 內建 retry 須設 0（max_retries=0）"


# ── 驗收標準 3：jitter 比例語意 + [0,1] + max_retries clamp ≥0 ──
def test_jitter_and_clamp_semantics():
    sec = arch_section()
    assert "jitter" in sec
    assert "比例" in sec, "未說明 jitter 為比例"
    assert "非秒數" in sec or "不是秒數" in sec, "未明示 jitter 非秒數"
    assert "[0,1]" in sec or "[0、1]" in sec, "未標 jitter 範圍 [0,1]"
    assert re.search(r"max_retries.{0,30}(clamp|夾|≥\s*0|>=\s*0)", sec), "未說明 max_retries clamp ≥0"


# ── 驗收標準 4：工廠上移接入點以決策形式標注 ──
def test_future_factory_relocation_decision():
    sec = arch_section()
    assert "伏筆" in sec or "決策" in sec, "未以決策/伏筆形式標注"
    assert "上移" in sec or "搬" in sec, "未提工廠上移"
    # 點名候選落點
    assert "providers.py" in sec and "llm_caller.py" in sec, "未標明上移候選（providers.py / llm_caller.py）"
    assert "第三" in sec, "未說明觸發條件（第三個 provider）"


# ── 驗收標準 5：錨點與現碼一致（反查原始碼，禁杜撰）──
def test_anchors_exist_in_source():
    assert "def make_retry_config" in read("studio/experts.py"), \
        "make_retry_config 不在 experts.py"
    llm = read("studio/llm_caller.py")
    assert "class RetryConfig" in llm, "RetryConfig 不在 llm_caller.py"
    assert "def run_with_retries" in llm, "run_with_retries 不在 llm_caller.py"
    assert "def as_kwargs" in llm, "as_kwargs 不在 llm_caller.py"
    prov = read("studio/providers.py")
    assert "max_retries=0" in prov, "providers.py 未見 max_retries=0"
    assert "make_retry_config" in prov, "providers.py 未 import/使用同一工廠"


def test_as_kwargs_packs_three_keys():
    """文件聲稱 as_kwargs 封裝 max_retries/backoff/sleep 三鍵——反查確認。"""
    llm = read("studio/llm_caller.py")
    body = llm[llm.index("def as_kwargs"):]
    body = body[:body.index("\n\n\n")] if "\n\n\n" in body else body[:600]
    for key in ('"max_retries"', '"backoff"', '"sleep"'):
        assert key in body, f"as_kwargs 未封裝 {key}"


def test_config_four_knobs_exist():
    cfg = read("studio/config.py")
    for name in ("EXPERT_RATE_LIMIT_RETRIES", "EXPERT_RATE_LIMIT_BACKOFF",
                 "EXPERT_RATE_LIMIT_BACKOFF_CAP", "EXPERT_RATE_LIMIT_BACKOFF_JITTER"):
        assert name in cfg, f"config.py 缺 {name}"


def test_clamp_actually_in_source():
    """文件聲稱 max_retries call-time clamp ≥0——工廠端與 __post_init__ 皆須有。"""
    assert "max(0, config.EXPERT_RATE_LIMIT_RETRIES)" in read("studio/experts.py"), \
        "工廠端未 clamp max_retries"
    assert "self.max_retries < 0" in read("studio/llm_caller.py"), \
        "RetryConfig.__post_init__ 未 clamp max_retries"


# ── 反向黑樣本：杜撰錨點不應出現在文件中 ──
def test_no_fabricated_anchors():
    sec = arch_section()
    for fake in ("Tenacity", "Stamina", "import backoff", "make_retry_policy",
                 "RetryPolicy", "retry_with_backoff"):
        assert fake not in sec, f"文件出現杜撰/未採用的錨點：{fake}"


# ── 驗收標準 6：模組職責表至少一列指回新節 ──
def test_module_table_links_back():
    txt = ARCH.read_text(encoding="utf-8")
    # 模組表中相關列須出現指回字樣
    table_refs = re.findall(r"^\| `(experts|llm_caller|providers)\.py` \|.*?LLM 韌性中介層.*$",
                            txt, re.M)
    assert table_refs, "模組職責表沒有任何一列指回新小節"


# ── 驗收標準：純文件變更，無 .py 被改動 ──
def test_no_py_changed():
    """護欄：本分支相對主幹基準不得改動 .py。

    注意：裸 `git diff`（working tree vs HEAD）在 commit 後永遠為空，是假綠燈護欄。
    這裡改用 `merge-base HEAD origin/main` 為基準對比 commit 後的實際變更，
    讓護欄真正反映「本分支引入了哪些 .py 變更」。
    """
    import pytest

    base = subprocess.run(
        ["git", "merge-base", "HEAD", "origin/main"],
        cwd=ROOT, capture_output=True, text=True,
    ).stdout.strip()
    if not base:
        pytest.skip("取不到 origin/main 基準，略過 .py 變更護欄（避免假綠）")
    out = subprocess.run(
        ["git", "diff", "--name-only", base, "HEAD", "--", "*.py"],
        cwd=ROOT, capture_output=True, text=True,
    )
    changed = [line for line in out.stdout.splitlines() if line.strip()]
    assert not changed, f"不應有 .py 被改動，卻動了：{changed}"
