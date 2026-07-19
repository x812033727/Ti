"""QA 獨立驗證（任務 #5）：providers 全呼叫點無第二套散傳退避，三端收斂於統一 RetryConfig 入口。

任務 #5 是「grep 全 providers 呼叫點」的可執行化——把字面 grep 提煉成 AST 靜態不變式
（純字串比對會被 docstring／註解誤導，故走 AST）外加一條 runtime 反向對照排假綠：

結構不變式（AST）：
- providers.py 不自建退避工廠／不複製 `RetryConfig`，只 `from .experts import make_retry_config`
  （單一來源，與 Claude 端共用同一 `EXPERT_RATE_LIMIT_*` 旋鈕）。
- providers.py 內 `run_with_retries` **只**出現在 `OpenAIExpert.speak`，且全檔僅一次；
  `complete_once` **不**自套第二層 `run_with_retries`（架構決策否決雙層退避）。
- providers.py 全檔無裸 `asyncio.sleep`／`time.sleep`——即無人繞過骨幹自寫退避迴圈。
- 三端（Claude=experts._speak_with_retries、OpenAI=providers.OpenAIExpert.speak）皆
  `cfg = make_retry_config()` 後 `run_with_retries(**cfg.as_kwargs(), ...)`。

runtime 反向對照（排「import 快照」假綠）：
- monkeypatch `config.EXPERT_RATE_LIMIT_RETRIES` 後，OpenAIExpert.speak 實際傳給
  `run_with_retries` 的 `max_retries` 隨之變化（call-time 讀 config，非模組載入快照）。
"""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path

import pytest

from studio import config, experts, llm_caller, providers
from studio.roles import Role

PROV_SRC = Path(providers.__file__).read_text(encoding="utf-8")
PROV_TREE = ast.parse(PROV_SRC)
EXP_SRC = Path(experts.__file__).read_text(encoding="utf-8")
EXP_TREE = ast.parse(EXP_SRC)


# ---------- AST 工具 ----------


def _funcs(tree: ast.AST):
    """回傳 name -> FunctionDef（含 async），名稱重複時保留最後一個。"""
    out: dict[str, ast.AST] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            out[node.name] = node
    return out


def _calls_named(node: ast.AST, name: str) -> int:
    """node 子樹內呼叫 `name(...)`（含 attribute 尾段 .name(...)）的次數。"""
    n = 0
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            f = sub.func
            if isinstance(f, ast.Name) and f.id == name:
                n += 1
            elif isinstance(f, ast.Attribute) and f.attr == name:
                n += 1
    return n


# ---------- 結構不變式 ----------


def test_providers_imports_shared_factory_not_own():
    """providers.py 必須從 experts 借 make_retry_config，且不自建工廠／不複製 RetryConfig。"""
    imported = False
    for node in ast.walk(PROV_TREE):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.endswith("experts"):
            if any(a.name == "make_retry_config" for a in node.names):
                imported = True
    assert imported, "providers.py 應 `from .experts import make_retry_config`（單一來源）"

    # 不得自己定義 make_retry_config／RetryConfig（避免另起一套旋鈕）
    defined = {
        n.name
        for n in ast.walk(PROV_TREE)
        if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
    }
    assert "make_retry_config" not in defined, "providers.py 不應自建退避工廠"
    assert "RetryConfig" not in defined, "providers.py 不應複製 RetryConfig"


def test_run_with_retries_only_in_openai_speak_once():
    """run_with_retries 只能出現在 OpenAIExpert.speak，且全檔僅一次（無散傳第二套退避）。"""
    total = _calls_named(PROV_TREE, "run_with_retries")
    assert total == 1, f"providers.py 內 run_with_retries 應恰好一處，實為 {total}"

    # 定位該唯一呼叫所屬的最內層函式名
    owners: list[str] = []
    for node in ast.walk(PROV_TREE):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            if _calls_named(node, "run_with_retries") and not any(
                isinstance(inner, ast.FunctionDef | ast.AsyncFunctionDef)
                and inner is not node
                and _calls_named(inner, "run_with_retries")
                for inner in ast.walk(node)
            ):
                owners.append(node.name)
    assert "speak" in owners, "唯一的 run_with_retries 應位於 OpenAIExpert.speak"


def test_complete_once_has_no_second_retry_layer():
    """complete_once 不得自套第二層 run_with_retries（架構決策否決雙層退避）。"""
    fns = _funcs(PROV_TREE)
    assert "complete_once" in fns, "找不到 complete_once"
    assert _calls_named(fns["complete_once"], "run_with_retries") == 0, (
        "complete_once 不應套第二層退避——退避是 speak() 層職責"
    )


def test_providers_has_no_raw_sleep_retry():
    """providers.py 全檔不得有裸 asyncio.sleep／time.sleep（即無人繞過骨幹自寫退避）。"""
    bad: list[str] = []
    for node in ast.walk(PROV_TREE):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            mod = node.func.value
            if (
                node.func.attr == "sleep"
                and isinstance(mod, ast.Name)
                and mod.id in {"asyncio", "time"}
            ):
                bad.append(f"{mod.id}.sleep@line{node.lineno}")
    assert not bad, f"providers.py 不應有裸 sleep 退避：{bad}"


@pytest.mark.parametrize(
    "tree,fn_name,label",
    [
        (PROV_TREE, "speak", "OpenAI 端 OpenAIExpert.speak"),
        (EXP_TREE, "_speak_with_retries", "Claude 端 experts._speak_with_retries"),
    ],
)
def test_both_speak_ends_use_unified_entry(tree, fn_name, label):
    """三端皆 cfg = make_retry_config() 後走 run_with_retries（統一入口）。"""
    fns = _funcs(tree)
    assert fn_name in fns, f"{label}: 找不到 {fn_name}"
    fn = fns[fn_name]
    assert _calls_named(fn, "make_retry_config") >= 1, f"{label} 應呼叫 make_retry_config()"
    assert _calls_named(fn, "run_with_retries") >= 1, f"{label} 應走 run_with_retries"


def test_no_stale_task2_pending_prose_in_providers():
    """providers.py 散文不得殘留「OpenAI 端退避待 task #2 併入／會無退避冒泡」這類過時敘述。

    task #2 已併入（OpenAIExpert.speak 已接 run_with_retries），此類字樣會與「三端收斂」
    結論直接矛盾、誤導除錯者排除「OpenAI 退避耗盡」這個真實成因。防其回潮。
    """
    # 純文字掃描（涵蓋 AST 看不到的 docstring／註解）；ERE 友善、不依賴 PCRE。
    stale_markers = ["待 task #2", "在其併入本 lane 前", "task #2 補上", "尚未吸收的限流"]
    hits = [m for m in stale_markers if m in PROV_SRC]
    assert not hits, f"providers.py 殘留過時 task#2 敘述（與三端收斂矛盾）：{hits}"


def test_make_retry_config_single_definition_in_experts():
    """make_retry_config 唯一定義在 experts.py（providers 共用之，非各自為政）。"""
    defs = [
        n
        for n in ast.walk(EXP_TREE)
        if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef) and n.name == "make_retry_config"
    ]
    assert len(defs) == 1, f"experts.py 應恰好一個 make_retry_config 定義，實為 {len(defs)}"


# ---------- runtime 反向對照（排 import 快照假綠） ----------


def _oneshot_role() -> Role:
    return Role(
        key="oneshot",
        name="收斂測試",
        avatar="🧪",
        title="t",
        model=config.MODEL_FAST,
        allowed_tools=[],
        permission_mode="default",
        system_prompt="s",
    )


class _Resp:
    def __init__(self, text: str):
        msg = type("M", (), {"content": text, "tool_calls": None})()
        self.choices = [type("C", (), {"message": msg})()]


def test_openai_end_max_retries_tracks_config_at_calltime(monkeypatch):
    """OpenAIExpert.speak 實際傳給 run_with_retries 的 max_retries 隨 config 變動而變——
    證實參數源自 call-time config（make_retry_config），非模組載入時的 import 快照。"""
    captured: list[int] = []

    async def _spy_run_with_retries(attempt_fn, *, max_retries, **kw):
        captured.append(max_retries)
        return await attempt_fn()

    monkeypatch.setattr(llm_caller, "run_with_retries", _spy_run_with_retries)
    # providers.py 以 `llm_caller.run_with_retries` 屬性存取，patch llm_caller 即生效

    async def _chat(messages, tools_, model):
        return _Resp("ok")

    async def _noop(_ev):
        return None

    async def _drive():
        for retries in (3, 9):
            monkeypatch.setattr(config, "EXPERT_RATE_LIMIT_RETRIES", retries)
            exp = providers.OpenAIExpert(_oneshot_role(), "sess", Path("."), _chat, "m")
            await exp.speak("hi", _noop)

    asyncio.run(_drive())

    assert captured == [3, 9], (
        f"max_retries 應隨 config 變動（call-time 讀取），實得 {captured}——"
        "若為固定值代表退避參數是 import 快照而非統一 config 入口"
    )
