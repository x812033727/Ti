"""QA 任務#2：驗證 ci.yml sandbox-test job 的「安裝 bubblewrap + 載入 AppArmor
bwrap-userns-restrict profile（找不到時自訂備援）」步驟符合驗收標準（結構面）。

apparmor_parser 屬 root/kernel 工具，本機 sandbox 無法載入；故此處做：
  (a) 安裝步驟確含 bubblewrap / apparmor 套件（+socat 防 fail-open 假 PASS）
  (b) AppArmor 步驟主路徑：extra-profiles → install → apparmor_parser -r
  (c) 備援路徑：SRC 不存在時寫自訂 profile 再 apparmor_parser -r
  (d) 備援 profile 字串的 AppArmor 語法結構正確（heredoc EOF 落行首、含 abi/
      profile bwrap /usr/bin/bwrap/userns 規則、各規則結尾逗號）
"""
import pathlib
import re

import pytest

yaml = pytest.importorskip("yaml")

CI = pathlib.Path(__file__).resolve().parent.parent / ".github" / "workflows" / "ci.yml"


@pytest.fixture(scope="module")
def steps():
    d = yaml.safe_load(CI.read_text())
    return d["jobs"]["sandbox-test"]["steps"]


def _step(steps, key):
    hit = [s for s in steps if key.lower() in s.get("name", "").lower()]
    assert hit, f"找不到名稱含 {key!r} 的步驟"
    return hit[0]


def _code_lines(run: str):
    """去掉 shell 註解行，只留實際指令。"""
    return [ln for ln in run.splitlines() if not ln.strip().startswith("#")]


# --- (a) 安裝步驟 -------------------------------------------------------


def test_install_step_has_bubblewrap_and_apparmor(steps):
    run = _step(steps, "Install system deps")["run"]
    code = "\n".join(_code_lines(run))
    assert "apt-get install" in code
    for pkg in ("bubblewrap", "apparmor-profiles", "apparmor-utils"):
        assert pkg in code, f"安裝步驟缺套件：{pkg}"


def test_install_step_includes_socat(steps):
    """socat 缺 → sandbox_missing_deps() 觸發 fail-open 靜默裸跑 → 假 PASS。"""
    run = _step(steps, "Install system deps")["run"]
    assert "socat" in "\n".join(_code_lines(run))


# --- (b) 主路徑：發行版自帶 profile -------------------------------------


def test_apparmor_step_primary_path(steps):
    run = _step(steps, "Enable bubblewrap")["run"]
    code = "\n".join(_code_lines(run))
    # 正確 profile 名稱
    assert "bwrap-userns-restrict" in code
    # 來源為發行版 extra-profiles
    assert "/usr/share/apparmor/extra-profiles/bwrap-userns-restrict" in code
    # 安裝到 /etc/apparmor.d 並 reload
    assert "/etc/apparmor.d/bwrap-userns-restrict" in code
    assert "install -m 0644" in code
    assert "apparmor_parser -r" in code


def test_apparmor_step_has_existence_branch(steps):
    """主/備援以 SRC 是否存在分流（找不到才走自訂備援）。"""
    code = "\n".join(_code_lines(_step(steps, "Enable bubblewrap")["run"]))
    assert re.search(r'if\s+\[\s+-f\s+"\$SRC"\s+\]', code), "缺少 [ -f $SRC ] 分支判斷"
    assert "else" in code and "fi" in code


# --- (c)(d) 備援路徑 + profile 語法結構 ---------------------------------


def _extract_fallback_profile(run: str) -> str:
    """抓出 heredoc (<<'EOF' ... EOF) 之間的備援 profile 內容。"""
    lines = run.splitlines()
    start = end = None
    for i, ln in enumerate(lines):
        if start is None and "<<'EOF'" in ln:
            start = i + 1
        elif start is not None and ln.strip() == "EOF":
            end = i
            break
    assert start is not None and end is not None, "找不到完整的 heredoc 備援 profile"
    return "\n".join(lines[start:end])


def test_fallback_uses_heredoc_and_reloads(steps):
    code = "\n".join(_code_lines(_step(steps, "Enable bubblewrap")["run"]))
    assert "tee" in code and "<<'EOF'" in code, "備援應以 here-doc 寫入 profile"
    # 寫入後同樣要 reload（主/備援共用最後一行 apparmor_parser -r "$DST"）
    assert "apparmor_parser -r" in code


def test_fallback_profile_heredoc_eof_at_col0(steps):
    """<<'EOF'（不帶 -）的閉合 EOF 必須在行首，否則 heredoc 不結束 → shell 壞掉。"""
    run = _step(steps, "Enable bubblewrap")["run"]
    assert any(ln == "EOF" for ln in run.splitlines()), "閉合 EOF 未落行首"


def test_fallback_profile_syntax_structure(steps):
    """備援 profile 須含 AppArmor 4.0 關鍵規則且結構正確，能被 apparmor_parser 吃下。"""
    prof = _extract_fallback_profile(_step(steps, "Enable bubblewrap")["run"])
    # abi 宣告（4.0，對應 Ubuntu 24.04 apparmor 4.x）
    assert re.search(r"abi\s+<abi/4\.0>,", prof), "缺 abi <abi/4.0>, 宣告"
    # tunables include
    assert re.search(r"include\s+<tunables/global>", prof), "缺 include <tunables/global>"
    # profile 標頭：名稱 bwrap、路徑 /usr/bin/bwrap、flags=(unconfined)
    assert re.search(
        r"profile\s+bwrap\s+/usr/bin/bwrap\s+flags=\(unconfined\)\s*\{", prof
    ), "profile 標頭格式不符"
    # userns 規則（放行 bwrap 自建 user namespace）須以逗號結尾
    assert re.search(r"\buserns\s*,", prof), "缺 userns, 規則（逗號結尾）"
    # local include 收尾
    assert re.search(r"include\s+if\s+exists\s+<local/bwrap>", prof), "缺 local/bwrap include"
    # 大括號成對
    assert prof.count("{") == prof.count("}") == 1, "profile 大括號未成對"
