"""repo clone 測試（不碰網路）：網址驗證、token 注入、token 遮蔽、注入防護。

注入／token／branch／sandbox 四項一律「攔截 run_command」：用 async spy 取代
runner.run_command，記錄 git_clone 實際組出的指令字串並回傳可控的假 RunOutput。
所有驗證僅靠攔截到的 command 字串與 tmp_path 副作用，全程不發起真實 clone。
"""

from __future__ import annotations

import asyncio
import shlex
from dataclasses import dataclass, field

import pytest

from studio import runner


@pytest.fixture(autouse=True)
def _forbid_real_subprocess(monkeypatch):
    """保險絲（PM #6 不碰網路）：本檔全程禁止啟動真實子程序。

    autouse 對本檔每個測試生效——把 asyncio 的 subprocess 建立函式換成會爆炸的
    版本。攔截 run_command 的測試根本走不到這裡；萬一日後有人誤加「真跑 clone」
    的測試，會立刻在此炸開而非默默連網，釘死「所有驗證僅靠攔截字串與 tmp_path」。
    """

    async def _boom(*args, **kwargs):
        raise RuntimeError("test forbids spawning a real subprocess (no network)")

    monkeypatch.setattr(asyncio, "create_subprocess_shell", _boom)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _boom)


@dataclass
class CloneSpy:
    """攔截 run_command 的 async spy：記錄呼叫並回傳可控的假 RunOutput。"""

    output: str = ""
    exit_code: int = 0
    command_label: str = "git clone (fake)"
    calls: list[dict] = field(default_factory=list)

    async def __call__(self, cwd, command, timeout=None, sandbox=None):
        self.calls.append({"cwd": cwd, "command": command, "timeout": timeout, "sandbox": sandbox})
        # 回傳的 command 故意給「非還原值」，以證明 git_clone 會無條件重設它（L313）
        return runner.RunOutput(
            command=self.command_label,
            exit_code=self.exit_code,
            output=self.output,
            timed_out=False,
        )

    @property
    def last(self) -> dict:
        assert self.calls, "run_command 未被呼叫"
        return self.calls[-1]


@pytest.fixture
def clone_spy(monkeypatch):
    """提供 spy 並完成共用 monkeypatch：

    - 以 spy 取代 runner.run_command（攔截路徑，git_clone 不會真的執行）
    - 強制 _git_available()==True，避免 git 缺失時 git_clone 早退使測試空跑
    四項（token／注入／branch／sandbox）測試共用，降低重複。
    """
    spy = CloneSpy()
    monkeypatch.setattr(runner, "run_command", spy)
    monkeypatch.setattr(runner, "_git_available", lambda: True)
    return spy


def test_is_valid_repo_url():
    assert runner.is_valid_repo_url("https://github.com/owner/repo")
    assert runner.is_valid_repo_url("https://github.com/owner/repo.git")
    assert runner.is_valid_repo_url("https://github.com/owner/repo/")
    # 僅接受 github.com 的 https
    assert not runner.is_valid_repo_url("http://github.com/owner/repo")
    assert not runner.is_valid_repo_url("https://evil.com/owner/repo")
    assert not runner.is_valid_repo_url("git@github.com:owner/repo.git")
    assert not runner.is_valid_repo_url("")


def test_build_clone_url_injects_token():
    url = "https://github.com/owner/repo.git"
    assert (
        runner.build_clone_url(url, "tok") == "https://x-access-token:tok@github.com/owner/repo.git"
    )
    assert runner.build_clone_url(url, None) == url
    assert runner.build_clone_url(url, "") == url


@pytest.mark.asyncio
async def test_git_clone_masks_token(clone_spy, tmp_path):
    """任務 #1：token 遮蔽。

    驗收 (PM #2)：餵入 token 後 result.output 與 result.command 都不含原始 token，
    且 command 還原為 `git clone <原始url>`（不含 x-access-token:）。
    """
    url = "https://github.com/owner/repo.git"
    token = "ghp_SECRETtoken1234567890"
    authed = runner.build_clone_url(url, token)  # https://x-access-token:<token>@...

    # spy 回傳的假 output 同時塞「裸 token」與「含 token 的 authed url」，
    # 確保 git_clone 的 result.output.replace(token, "***")（L312）兩處都清掉。
    clone_spy.output = (
        f"Cloning into '.'...\nfatal: could not read from {authed}\nraw token leaked: {token}\n"
    )

    result = await runner.git_clone(url, tmp_path, token=token)

    # --- output 遮蔽：原始 token 不得殘留，兩處出現位置都應被換成 *** ---
    assert token not in result.output
    assert "***" in result.output
    # authed url 內的 token 段也被遮（url 結構還在，但 token 沒了）
    assert f"x-access-token:{token}" not in result.output

    # --- command 還原：精確等值，且絕不含 token / x-access-token: ---
    # 註：command 是 git_clone 無條件重設為原始 url（L313），非遮蔽邏輯。
    assert result.command == "git clone " + url
    assert token not in result.command
    assert "x-access-token:" not in result.command


@pytest.mark.asyncio
async def test_git_clone_no_token_passthrough(clone_spy, tmp_path):
    """無 token 時不應出錯，command 仍還原為原始 url。"""
    url = "https://github.com/owner/repo"
    clone_spy.output = "ok"

    result = await runner.git_clone(url, tmp_path, token=None)

    assert result.command == "git clone " + url
    assert "x-access-token:" not in result.command
    # 攔截到的實際指令不含 token 注入（公開倉庫）
    assert "x-access-token:" not in clone_spy.last["command"]


# --- 任務 #2：url 注入防護（PM #3）---------------------------------------
# 經典 shell 注入 payload，嵌在 url 尾段；payload 試圖讓 shell 額外執行 touch pwned。
_URL_INJECTION_PAYLOADS = [
    "https://github.com/owner/repo`touch pwned`.git",  # backtick 命令替換
    "https://github.com/owner/repo$(touch pwned).git",  # $(...) 命令替換
    "https://github.com/owner/repo;touch pwned",  # ; 串接
    "https://github.com/owner/repo && touch pwned",  # && 串接
    "https://github.com/owner/repo|touch pwned",  # 管線
    "https://github.com/owner/repo\ntouch pwned",  # 換行注入
]


@pytest.mark.asyncio
@pytest.mark.parametrize("payload_url", _URL_INJECTION_PAYLOADS)
async def test_git_clone_url_injection_is_quoted(clone_spy, tmp_path, payload_url):
    """惡意 url payload 必須被 shlex.quote 跳脫成「單一 argv 元素」，不被 shell 解析。

    主斷言（真正釘住防線的證據）：攔截到的指令字串中，url 已被 shlex.quote 包覆，
    且用 shlex.split 重新解析後，payload 仍是「完整單一 token」，shell 不會把它
    拆成額外的 touch / pwned 等指令。
    """
    await runner.git_clone(payload_url, tmp_path, token=None)
    cmd = clone_spy.last["command"]

    # 無 token 時 authed == 原始 url；git_clone 以 shlex.quote(authed) 組進指令。
    authed = runner.build_clone_url(payload_url, None)
    assert authed == payload_url

    # 主斷言 1：指令字串確實包含「被引用後」的 url 片段。
    assert shlex.quote(payload_url) in cmd

    # 主斷言 2：以 shell 詞法還原指令，payload 必須是單一完整 token；
    # 若跳脫失效，shell 會把 `touch pwned` 拆出來變成獨立 token。
    tokens = shlex.split(cmd)
    assert payload_url in tokens
    assert "touch" not in tokens  # payload 沒被拆成獨立指令
    assert "pwned" not in tokens
    # 指令骨架不變：git clone --depth 1 <url> .
    assert tokens[:4] == ["git", "clone", "--depth", "1"]
    assert tokens[-1] == "."

    # 輔助防呆（非主證據）：因為攔截了 run_command，payload 根本沒被執行，
    # 哨兵檔天然不存在——此斷言 trivially true，僅作為「確實沒誤跑真實指令」的防呆。
    assert not (tmp_path / "pwned").exists()


@pytest.mark.asyncio
async def test_git_clone_branch_injection_blocked(clone_spy, tmp_path):
    """惡意 branch payload（含 shell metachar）被 _BRANCH_RE 拒絕，不進入指令。

    branch 注入由 _BRANCH_RE（^[\\w./-]+$）擋下：含 backtick/$/;/空格/換行的
    branch 不通過，git_clone 不附加 --branch，payload 完全不出現在指令中。
    （參數注入如 --upload-pack 的更完整覆蓋見任務 #4 的 _BRANCH_RE 測試。）
    """
    url = "https://github.com/owner/repo.git"
    for bad_branch in [
        "main;touch pwned",
        "main`touch pwned`",
        "$(touch pwned)",
        "main && touch pwned",
        "main\ntouch pwned",
    ]:
        await runner.git_clone(url, tmp_path, token=None, branch=bad_branch)
        cmd = clone_spy.last["command"]
        # branch 被拒：不附 --branch，且 payload 任何片段都不在指令裡。
        assert "--branch" not in cmd
        assert "touch" not in cmd
        assert "pwned" not in cmd
        # 輔助防呆（非主證據，攔截後 payload 未執行）。
        assert not (tmp_path / "pwned").exists()


# --- 任務 #3：_BRANCH_RE 參數注入過濾（PM #4）---------------------------
# 重點：shlex.quote 只防「命令注入」，不防「參數注入」——以 -- 開頭的 branch 即使
# 被正確引用，仍會被 git 當成選項（如 --upload-pack=<cmd> 可導致 RCE）。git_clone
# 靠 _BRANCH_RE（^[\w./-]{1,200}$）把這類 branch 整個擋掉、不附加 --branch。
_BAD_BRANCHES = [
    "--upload-pack=touch pwned",  # 經典參數注入 → RCE
    "--upload-pack=sh",
    "-o",  # 短選項
    "--foo",  # 任意長選項
    "main branch",  # 含空格
    "main;rm -rf",  # 含 metachar
    "main`x`",
    "",  # 空字串
    "x" * 201,  # 超長（>200）
]


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_branch", _BAD_BRANCHES)
async def test_git_clone_branch_re_rejects(clone_spy, tmp_path, bad_branch):
    """負向：不合法／危險 branch 被 _BRANCH_RE 拒絕 → 指令不附加 --branch。

    釘住此防線：日後若 regex 放寬導致 --upload-pack 之類能通過，本測試會立刻變紅。
    """
    await runner.git_clone(
        "https://github.com/owner/repo.git", tmp_path, token=None, branch=bad_branch
    )
    cmd = clone_spy.last["command"]
    assert "--branch" not in cmd
    # payload 不應以任何形式進入指令（空字串例外，本就無內容可比對）。
    if bad_branch:
        assert bad_branch not in cmd


_GOOD_BRANCHES = ["main", "feat/x", "release/1.0", "v1.2.3", "feature_a", "a.b-c"]


@pytest.mark.asyncio
@pytest.mark.parametrize("good_branch", _GOOD_BRANCHES)
async def test_git_clone_branch_re_accepts(clone_spy, tmp_path, good_branch):
    """正向：合法 branch 被接受 → 指令以 `--branch <branch>` 相鄰附加且經 shlex.quote。

    與負向成對，避免日後把 regex 收太緊而誤殺正常 branch。
    """
    await runner.git_clone(
        "https://github.com/owner/repo.git", tmp_path, token=None, branch=good_branch
    )
    cmd = clone_spy.last["command"]
    assert "--branch" in cmd
    # shlex.split 還原後，--branch 與 branch 值必須相鄰且完全一致。
    tokens = shlex.split(cmd)
    idx = tokens.index("--branch")
    assert tokens[idx + 1] == good_branch


# --- 任務 #4：強制非沙箱（PM #5）---------------------------------------
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "token, branch",
    [
        (None, None),  # 公開倉庫、無 branch
        ("ghp_xxx", None),  # 私有倉庫
        (None, "main"),  # 帶 branch
        ("ghp_xxx", "feat/x"),  # token + branch 全帶
    ],
)
async def test_git_clone_forces_no_sandbox(clone_spy, tmp_path, token, branch):
    """git_clone 必須一律以 sandbox=False 呼叫 run_command。

    clone 需要網路，而沙箱預設斷網（--unshare-net），若進沙箱會直接失敗。
    對應 runner.git_clone L309 的硬編 `sandbox=False`，任何參數組合都不得改變。
    """
    await runner.git_clone(
        "https://github.com/owner/repo.git", tmp_path, token=token, branch=branch
    )
    assert clone_spy.last["sandbox"] is False


# --- 任務 #5：不碰網路（PM #6）-----------------------------------------
@pytest.mark.asyncio
async def test_git_clone_never_spawns_real_subprocess(monkeypatch, tmp_path):
    """釘死「不碰網路」：故意「不」攔截 run_command，讓 git_clone 走真實路徑。

    唯一對外（連網）的途徑就是 run_command 內的 asyncio 子程序；本檔的 autouse
    保險絲已把它換成會炸的版本。因此 git_clone 必然在嘗試 spawn 時 RuntimeError，
    證明：(1) 對外只有子程序這一條路、(2) 本檔已封死、(3) tmp_path 無任何 clone 產物。
    """
    # 強制 _git_available()=True 確保走到 spawn 點（與環境是否裝 git 脫鉤）。
    monkeypatch.setattr(runner, "_git_available", lambda: True)

    with pytest.raises(RuntimeError, match="real subprocess"):
        await runner.git_clone("https://github.com/owner/repo.git", tmp_path)

    # 沒有任何 clone 產物落地。
    assert list(tmp_path.iterdir()) == []
