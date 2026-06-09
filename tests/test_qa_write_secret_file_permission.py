"""QA：`write_secret_file` 安全寫入驗證。

對齊驗收標準 1~7：
 1. studio 內存在 write_secret_file；實作不含 os.umask 暫存交換寫法；有 fchmod 0600。
 2. 新建秘密檔精確 0o600，與行程 umask 無關（umask=0 時仍 0600）。
 3. 既存 0644 .env 經 write_secret_file 後收緊為 0600。
 4. 內容正確、os.environ 同步、config.reload() 後生效（settings 接線）。
 5. 多執行緒併發：N 緒同寫後恆 0600，無 group/other 可讀位。
 6. 不引入新第三方套件（僅 os/tempfile）；symlink 不被跟隨。
 7. （由整體測試套件保證無回歸）
"""

from __future__ import annotations

import inspect
import os
import stat
import threading

import pytest

from studio import secretfile
from studio.secretfile import write_secret_file


# ---------------------------------------------------------------------------
# 驗收 #1：實作靜態檢查
# ---------------------------------------------------------------------------
def test_function_exists_in_studio():
    assert callable(write_secret_file)
    assert write_secret_file.__module__.startswith("studio")


def test_impl_no_umask_swap_and_has_fchmod_or_chmod():
    src = inspect.getsource(secretfile)
    # 不得有 os.umask( 的暫存交換寫法
    assert "os.umask(" not in src, "實作不應使用 os.umask 暫存交換寫法"
    # 必須有 fchmod 或 (mkstemp/replace) 或結尾 chmod 0o600 之一保證權限
    assert ("fchmod" in src) or ("mkstemp" in src and "replace" in src), (
        "需有 os.fchmod 或 mkstemp+os.replace 的安全建檔"
    )
    assert "0o600" in src


def test_no_new_third_party_imports():
    """驗收 #6：只允許標準庫 + 既有 dotenv，不得引入新第三方套件。"""
    src = inspect.getsource(secretfile)
    # 粗略掃描 import 行
    forbidden = ["atomicwrites", "filelock", "portalocker"]
    for mod in forbidden:
        assert mod not in src, f"不應引入新套件 {mod}"


# ---------------------------------------------------------------------------
# 驗收 #2：新建檔精確 0600，與 umask 無關
# ---------------------------------------------------------------------------
def _mode(path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


def test_new_file_is_exactly_0600(tmp_path):
    p = str(tmp_path / ".env")
    write_secret_file(p, "GITHUB_TOKEN", "ghp_new_0600")
    assert _mode(p) == 0o600, f"新建檔應為 0600，實為 {oct(_mode(p))}"
    assert "ghp_new_0600" in open(p).read()


def test_new_file_0600_even_when_umask_zero(tmp_path):
    """umask 設為 0（最寬鬆）時仍須精確 0600 —— 與 umask 脫鉤。"""
    p = str(tmp_path / "umask0.env")
    old = os.umask(0)
    try:
        write_secret_file(p, "OPENAI_API_KEY", "sk-umask0")
    finally:
        os.umask(old)
    m = _mode(p)
    assert m == 0o600, f"umask=0 時仍須 0600，實為 {oct(m)}"
    assert m & 0o077 == 0, "不得有 group/other 任何權限位"


# ---------------------------------------------------------------------------
# 驗收 #3：既存 0644 檔被收緊
# ---------------------------------------------------------------------------
def test_existing_0644_is_tightened(tmp_path):
    p = str(tmp_path / "loose.env")
    with open(p, "w") as fh:
        fh.write("EXISTING=1\n")
    os.chmod(p, 0o644)
    assert _mode(p) == 0o644
    write_secret_file(p, "GITHUB_TOKEN", "ghp_tighten")
    assert _mode(p) == 0o600, f"既存 0644 應收緊為 0600，實為 {oct(_mode(p))}"
    body = open(p).read()
    assert "EXISTING=1" in body and "ghp_tighten" in body, "既有內容與新 key 都應保留"


def test_existing_0666_is_tightened(tmp_path):
    p = str(tmp_path / "veryloose.env")
    with open(p, "w") as fh:
        fh.write("A=1\n")
    os.chmod(p, 0o666)
    write_secret_file(p, "B", "2")
    assert _mode(p) == 0o600


# ---------------------------------------------------------------------------
# 驗收 #4：內容正確 + 更新既有 key
# ---------------------------------------------------------------------------
def test_value_update_overwrites_same_key(tmp_path):
    p = str(tmp_path / ".env")
    write_secret_file(p, "TOKEN", "v1")
    write_secret_file(p, "TOKEN", "v2")
    body = open(p).read()
    assert "v2" in body
    assert body.count("TOKEN") == 1, "同 key 應被更新而非重複"
    assert _mode(p) == 0o600


# ---------------------------------------------------------------------------
# 驗收 #5：多執行緒併發，恆 0600，無 world/group readable
# ---------------------------------------------------------------------------
def test_concurrent_writes_never_world_readable(tmp_path):
    p = str(tmp_path / "concurrent.env")
    N = 24
    barrier = threading.Barrier(N)
    bad_modes: list[int] = []
    lock = threading.Lock()
    stop = threading.Event()

    def watcher():
        # 在寫入期間持續抽查權限，捕捉任何 group/other 可讀窗口
        while not stop.is_set():
            try:
                m = _mode(p)
                if m & 0o077:
                    with lock:
                        bad_modes.append(m)
            except FileNotFoundError:
                pass

    def worker(i: int):
        barrier.wait()
        write_secret_file(p, f"KEY_{i}", f"val_{i}")
        m = _mode(p)
        if m & 0o077:
            with lock:
                bad_modes.append(m)

    w = threading.Thread(target=watcher, daemon=True)
    w.start()
    threads = [threading.Thread(target=worker, args=(i,)) for i in range(N)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    stop.set()
    w.join(timeout=2)

    assert _mode(p) == 0o600, f"併發結束後應為 0600，實為 {oct(_mode(p))}"
    assert not bad_modes, f"併發期間出現 world/group 可讀位：{[oct(m) for m in bad_modes]}"
    # 所有 key 都應寫入（lost-update 不該發生）
    body = open(p).read()
    for i in range(N):
        assert f"KEY_{i}=" in body, f"KEY_{i} 遺失（疑似 lost update）"


# ---------------------------------------------------------------------------
# 驗收 #6：symlink 不被跟隨
# ---------------------------------------------------------------------------
def test_symlink_not_followed(tmp_path):
    """指向外部敏感目標的 symlink 不應被當成寫入目標跟隨過去。"""
    target = tmp_path / "outside_target"
    target.write_text("SENSITIVE=keep\n")
    os.chmod(str(target), 0o600)
    link = tmp_path / "link.env"
    os.symlink(str(target), str(link))

    write_secret_file(str(link), "INJECT", "x")

    # 不跟隨 symlink：原 target 不應被注入新 key
    assert "INJECT" not in target.read_text(), "symlink 被跟隨了，污染到 target"


# ---------------------------------------------------------------------------
# 驗收 #4（接線）：settings.update 寫入後 os.environ 同步、config.reload 生效
#   並驗證寫出的 .env 權限為 0600（呼叫端是否經由 write_secret_file 的實質檢查）
# ---------------------------------------------------------------------------
@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    from studio import config, settings

    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    keys = [f.env for f in settings.FIELDS]
    saved = {k: os.environ.get(k) for k in keys}
    yield tmp_path, settings, config
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    config.reload()


def test_settings_update_writes_0600_env(sandbox):
    tmp_path, settings, config = sandbox
    settings.update({"GITHUB_TOKEN": "ghp_via_settings"})
    env = tmp_path / ".env"
    assert env.exists()
    assert os.environ["GITHUB_TOKEN"] == "ghp_via_settings"
    assert "ghp_via_settings" in env.read_text()
    # 驗收 #2/#3 的接線效果：經由設定 API 寫出的 .env 也應為 0600
    assert _mode(str(env)) == 0o600, (
        f"settings.update 寫出的 .env 權限應為 0600，實為 {oct(_mode(str(env)))}；"
        "若仍為寬鬆權限，代表呼叫端尚未改用 write_secret_file"
    )


def test_settings_update_tightens_existing_0644_env(sandbox):
    """真實破口：既存 0644 的 .env 經 settings.update（呼叫端）後須收緊為 0600。

    這是上一輪的失敗點——若 settings.update 仍裸用 set_key，既存 0644 不會收緊。
    """
    tmp_path, settings, config = sandbox
    env = tmp_path / ".env"
    env.write_text("OPENAI_API_KEY=preexisting\n")
    os.chmod(str(env), 0o644)
    assert _mode(str(env)) == 0o644

    settings.update({"GITHUB_TOKEN": "ghp_tighten_via_api"})

    assert _mode(str(env)) == 0o600, (
        f"既存 0644 經 settings.update 後應收緊為 0600，實為 {oct(_mode(str(env)))}"
    )
    body = env.read_text()
    assert "ghp_tighten_via_api" in body and "preexisting" in body


def test_auth_set_password_writes_0600_env(sandbox):
    """auth.set_password 接線檢查：寫密碼亦走安全寫入，既存 0644 收緊為 0600。"""
    tmp_path, settings, config = sandbox
    from studio import auth

    env = tmp_path / ".env"
    env.write_text("FOO=bar\n")
    os.chmod(str(env), 0o644)

    auth.set_password("s3cret-pw")

    assert _mode(str(env)) == 0o600, (
        f"auth.set_password 後 .env 應為 0600，實為 {oct(_mode(str(env)))}"
    )
    assert "TI_ACCESS_PASSWORD" in env.read_text()
    assert os.environ["TI_ACCESS_PASSWORD"] == "s3cret-pw"


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main([__file__, "-v"]))
