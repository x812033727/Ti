"""repo_base（目標 repo＝工作基底）：同步狀態機與 ensure_base 短路/遮蔽。

兩種風格並用：
- argv-spy（比照 tests/test_clone.py）：攔截 runner.run_command_exec，驗證 clone/fetch
  的 argv 組裝、token 遮蔽、label 不帶 URL——全程不發起真實子程序、不碰網路。
- 真 git 整合：以 file:///…/bare.git 當 url 餵 sync_workspace，走真 git 但零網路，
  驗證 ff/up_to_date/local_ahead/forked/diverged/unborn 等狀態與「絕不清空」鐵則。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from studio import config, git_cred, repo_base, runner

TOKEN = "ghp_SECRETtoken1234567890"


# --- argv-spy 風格（不碰網路）------------------------------------------


class ExecSpy:
    """攔截 run_command_exec：記錄 argv，依 argv 開頭回傳預設的假 RunOutput。"""

    def __init__(self):
        self.calls: list[dict] = []
        self.outputs: dict[str, tuple[int, str]] = {}  # argv[1] -> (exit_code, output)

    async def __call__(self, cwd, argv, timeout=None, sandbox=None, label=None, env=None):
        self.calls.append(
            {"cwd": cwd, "argv": list(argv), "sandbox": sandbox, "label": label, "env": env}
        )
        code, out = self.outputs.get(argv[1], (0, ""))
        return runner.RunOutput(
            command=label or argv[0], exit_code=code, output=out, timed_out=False
        )

    def by_sub(self, sub: str) -> list[dict]:
        return [c for c in self.calls if c["argv"][1] == sub]


@pytest.fixture(autouse=True)
def _forbid_real_subprocess_in_spy_tests(request, monkeypatch):
    """spy 類測試禁真子程序（比照 test_clone.py 的保險絲）；真 git 整合測試以
    `realgit` marker 豁免——它們刻意走本機 file:// 真 git（仍零網路）。"""
    if request.node.get_closest_marker("realgit"):
        return
    import asyncio

    async def _boom(*args, **kwargs):
        raise RuntimeError("test forbids spawning a real subprocess (no network)")

    monkeypatch.setattr(asyncio, "create_subprocess_shell", _boom)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _boom)


@pytest.fixture
def spy(monkeypatch):
    s = ExecSpy()
    monkeypatch.setattr(runner, "run_command_exec", s)
    monkeypatch.setattr(runner, "_git_available", lambda: True)
    monkeypatch.setattr(config, "OFFLINE_MODE", False)
    monkeypatch.setattr(config, "ENABLE_GIT", True)
    monkeypatch.setattr(config, "GITHUB_TOKEN", TOKEN)
    monkeypatch.setattr(config, "PUBLISH_BASE", "main")
    monkeypatch.setattr(config, "TI_GIT_CRED_LEGACY", False)
    monkeypatch.setattr(git_cred, "_GIT_ENV_SUPPORTED", True)
    return s


async def test_ensure_base_skips_without_repo(spy, tmp_path):
    r = await repo_base.ensure_base(tmp_path, "")
    assert r.status == "skipped" and not r.based and not r.fatal
    assert spy.calls == []


@pytest.mark.parametrize("knob", ["OFFLINE_MODE", "ENABLE_GIT"])
async def test_ensure_base_skips_when_disabled(spy, tmp_path, monkeypatch, knob):
    monkeypatch.setattr(config, knob, knob == "OFFLINE_MODE")  # 離線=True / git=False
    r = await repo_base.ensure_base(tmp_path, "me/product")
    assert r.status == "skipped"
    assert spy.calls == []


async def test_pristine_clone_argv_full_depth_with_token(spy, tmp_path):
    """全新 workspace：完整 clone、--branch base、token 走 env、label 無 URL。"""
    ws = tmp_path / "ws"
    r = await repo_base.ensure_base(ws, "me/product")
    assert r.status == "cloned" and r.based and not r.fatal

    clones = spy.by_sub("clone")
    assert len(clones) == 1
    argv = clones[0]["argv"]
    assert "--depth" not in argv  # 基底要完整歷史（merge-base 判定/git log）
    idx = argv.index("--branch")
    assert argv[idx + 1] == "main"
    assert "https://github.com/me/product" in argv
    assert not any(TOKEN in p or "x-access-token:" in p for p in argv)
    assert clones[0]["env"]["GIT_CONFIG_KEY_1"] == "http.https://github.com/.extraheader"
    assert TOKEN not in "".join(clones[0]["env"].values())
    assert clones[0]["label"] == "git clone"  # token URL 絕不進 label
    assert clones[0]["sandbox"] is False  # clone 需網路，沙箱預設斷網


async def test_pristine_remote_missing_starts_blank(spy, tmp_path):
    """repo 不存在/無 base 分支：remote_unavailable、workspace 維持空白（首發佈接手）。"""
    spy.outputs["clone"] = (128, "fatal: remote: Repository not found.")
    ws = tmp_path / "ws"
    r = await repo_base.ensure_base(ws, "me/ghost")
    assert r.status == "remote_unavailable" and not r.based and not r.fatal
    assert list(ws.iterdir()) == []  # 沒有半成品殘留


async def test_pristine_clone_error_is_fatal_and_redacted(spy, tmp_path):
    """全新 workspace 拿不到基底（憑證/網路）→ fatal；detail 不得洩 token。"""
    spy.outputs["clone"] = (
        128,
        f"fatal: unable to access 'https://x-access-token:{TOKEN}@github.com/me/product/'",
    )
    r = await repo_base.ensure_base(tmp_path / "ws", "me/product")
    assert r.status == "error" and r.fatal
    assert TOKEN not in r.detail


async def test_fetch_label_and_detail_carry_no_token(spy, tmp_path):
    """has_history 路徑的 fetch：乾淨 URL 進 argv，token 只走 env。"""
    ws = tmp_path / "ws"
    (ws / ".git").mkdir(parents=True)  # 偽 .git；rev-parse 由 spy 回 ok → has_history
    (ws / "a.txt").write_text("x", encoding="utf-8")
    r = await repo_base.ensure_base(ws, "me/product")
    # spy 對 rev-parse 回相同輸出 → HEAD == FETCH_HEAD → up_to_date
    assert r.status == "up_to_date" and r.based

    fetches = spy.by_sub("fetch")
    assert len(fetches) == 1
    assert fetches[0]["label"] == "git fetch"
    assert "https://github.com/me/product" in fetches[0]["argv"]
    assert not any(TOKEN in p or "x-access-token:" in p for p in fetches[0]["argv"])
    assert fetches[0]["env"]["GIT_CONFIG_KEY_1"] == "http.https://github.com/.extraheader"
    assert TOKEN not in "".join(fetches[0]["env"].values())
    assert all(TOKEN not in (c["label"] or "") for c in spy.calls)


async def test_fetch_legacy_uses_token_url(spy, tmp_path, monkeypatch):
    """legacy 閥開啟時，fetch 回退舊 token-in-URL。"""
    monkeypatch.setattr(config, "TI_GIT_CRED_LEGACY", True)
    ws = tmp_path / "ws"
    (ws / ".git").mkdir(parents=True)
    (ws / "a.txt").write_text("x", encoding="utf-8")

    r = await repo_base.ensure_base(ws, "me/product")

    assert r.status == "up_to_date" and r.based
    fetch = spy.by_sub("fetch")[0]
    assert f"https://x-access-token:{TOKEN}@github.com/me/product" in fetch["argv"]
    assert not fetch["env"]


async def test_fetch_non_github_host_does_not_add_credentials(spy, tmp_path):
    ws = tmp_path / "ws"
    (ws / ".git").mkdir(parents=True)
    (ws / "a.txt").write_text("x", encoding="utf-8")

    r = await repo_base.sync_workspace(
        ws,
        "https://example.com/me/product",
        "main",
        token=TOKEN,
    )

    assert r.status == "up_to_date" and r.based
    fetch = spy.by_sub("fetch")[0]
    assert "https://example.com/me/product" in fetch["argv"]
    assert not any(TOKEN in p or "x-access-token:" in p for p in fetch["argv"])
    assert not fetch["env"]


async def test_fetch_failure_is_nonfatal(spy, tmp_path):
    """非首次（已有歷史）fetch 失敗：remote_unavailable，本地照常續作。"""
    spy.outputs["fetch"] = (128, "fatal: could not read from remote repository")
    ws = tmp_path / "ws"
    (ws / ".git").mkdir(parents=True)
    (ws / "a.txt").write_text("x", encoding="utf-8")
    r = await repo_base.ensure_base(ws, "me/product")
    assert r.status == "remote_unavailable" and not r.fatal


# --- 真 git 整合（file:// 零網路）---------------------------------------


@pytest.fixture
def _realgit_env(monkeypatch):
    """真 git 測試的確定性環境：git 開、沙箱關（容器可能沒 bwrap，沙箱 fail-closed
    會讓「自動 commit 收殘留」假性失敗）、不離線。"""
    monkeypatch.setattr(config, "ENABLE_GIT", True)
    monkeypatch.setattr(config, "SANDBOX_ENABLED", False)
    monkeypatch.setattr(config, "OFFLINE_MODE", False)


def _git(cwd: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=T", "-c", "commit.gpgsign=false", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.strip()


def _commit_count(cwd: Path) -> int:
    return int(_git(cwd, "rev-list", "--count", "HEAD"))


@pytest.fixture
def remote(tmp_path, _realgit_env):
    """本機 bare repo 當「目標 repo」：main 分支、一個檔案、一個 commit。"""
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-q", "-b", "main")
    (seed / "README.md").write_text("v1\n", encoding="utf-8")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-q", "-m", "init")
    bare = tmp_path / "bare.git"
    _git(tmp_path, "clone", "-q", "--bare", str(seed), str(bare))
    return {"bare": bare, "seed": seed, "url": bare.as_uri()}


def _advance_remote(remote: dict, text: str) -> None:
    (remote["seed"] / "README.md").write_text(text, encoding="utf-8")
    _git(remote["seed"], "add", "-A")
    _git(remote["seed"], "commit", "-q", "-m", f"remote: {text.strip()}")
    _git(remote["seed"], "push", "-q", str(remote["bare"]), "main")


@pytest.mark.realgit
async def test_real_pristine_clone_then_up_to_date(remote, tmp_path):
    ws = tmp_path / "ws"
    r = await repo_base.sync_workspace(ws, remote["url"], "main")
    assert r.status == "cloned" and r.based
    assert (ws / "README.md").read_text(encoding="utf-8") == "v1\n"

    r2 = await repo_base.sync_workspace(ws, remote["url"], "main")
    assert r2.status == "up_to_date"


@pytest.mark.realgit
async def test_real_fast_forward_after_merge_and_branch_rename(remote, tmp_path):
    """遠端前進（上場 PR 已合併）→ 快轉；上場 publish 改掉的分支名被正規化回 base。"""
    ws = tmp_path / "ws"
    await repo_base.sync_workspace(ws, remote["url"], "main")
    before = _commit_count(ws)
    _git(ws, "branch", "-M", "ti-studio/lastsession")  # 模擬 publisher._push 的改名殘留
    _advance_remote(remote, "v2\n")

    r = await repo_base.sync_workspace(ws, remote["url"], "main")
    assert r.status == "fast_forwarded" and r.based
    assert (ws / "README.md").read_text(encoding="utf-8") == "v2\n"
    assert _commit_count(ws) == before + 1
    assert _git(ws, "branch", "--show-current") == "main"


@pytest.mark.realgit
async def test_real_local_ahead_kept(remote, tmp_path):
    """本地領先（上場 PR 未合併）：不動本地，成果疊進下個 PR。"""
    ws = tmp_path / "ws"
    await repo_base.sync_workspace(ws, remote["url"], "main")
    (ws / "new.txt").write_text("work\n", encoding="utf-8")
    _git(ws, "add", "-A")
    _git(ws, "commit", "-q", "-m", "local work")
    head = _git(ws, "rev-parse", "HEAD")

    r = await repo_base.sync_workspace(ws, remote["url"], "main")
    assert r.status == "local_ahead" and r.based
    assert _git(ws, "rev-parse", "HEAD") == head  # 完全沒動


@pytest.mark.realgit
async def test_real_dirty_leftover_committed_then_forked(remote, tmp_path):
    """上場中斷殘留（未提交變更）＋遠端已前進：先自動 commit 保住變更，
    判為 forked（仍同源、PR 開得成），絕不遺失任何東西。"""
    ws = tmp_path / "ws"
    await repo_base.sync_workspace(ws, remote["url"], "main")
    (ws / "wip.txt").write_text("殘留\n", encoding="utf-8")
    _advance_remote(remote, "v2\n")

    r = await repo_base.sync_workspace(ws, remote["url"], "main")
    assert r.status == "forked" and r.based
    assert (ws / "wip.txt").read_text(encoding="utf-8") == "殘留\n"
    assert _git(ws, "status", "--porcelain") == ""  # 殘留已被 commit 收掉


@pytest.mark.realgit
async def test_real_diverged_keeps_local_intact(remote, tmp_path):
    """鐵則：獨立歷史的 workspace（設定目標 repo 前就存在）絕不被清空/覆蓋。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    _git(ws, "init", "-q", "-b", "main")
    (ws / "own.py").write_text("print('mine')\n", encoding="utf-8")
    _git(ws, "add", "-A")
    _git(ws, "commit", "-q", "-m", "independent root")
    before_head = _git(ws, "rev-parse", "HEAD")
    before_count = _commit_count(ws)

    r = await repo_base.sync_workspace(ws, remote["url"], "main")
    assert r.status == "diverged" and not r.based and not r.fatal
    assert _git(ws, "rev-parse", "HEAD") == before_head
    assert _commit_count(ws) == before_count
    assert (ws / "own.py").read_text(encoding="utf-8") == "print('mine')\n"
    assert not (ws / "README.md").exists()  # 遠端內容沒有被混進來


@pytest.mark.realgit
async def test_real_unborn_git_gets_base_injected(remote, tmp_path):
    """init 過但零 commit 零散檔：等價於 pristine，注入遠端 base 當基底。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    _git(ws, "init", "-q", "-b", "main")

    r = await repo_base.sync_workspace(ws, remote["url"], "main")
    assert r.status == "cloned" and r.based
    assert (ws / "README.md").read_text(encoding="utf-8") == "v1\n"
    assert _git(ws, "branch", "--show-current") == "main"


@pytest.mark.realgit
async def test_real_local_files_without_git_untouched(remote, tmp_path):
    """有檔案但無 .git：一律不碰（diverged），檔案原樣保留。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "data.txt").write_text("珍貴資料\n", encoding="utf-8")

    r = await repo_base.sync_workspace(ws, remote["url"], "main")
    assert r.status == "diverged"
    assert (ws / "data.txt").read_text(encoding="utf-8") == "珍貴資料\n"
    assert not (ws / ".git").exists()


@pytest.mark.realgit
async def test_real_empty_remote_is_unavailable(tmp_path, _realgit_env):
    """空 bare repo（無 base 分支）：clone 失敗歸 remote_unavailable，從空白開始。"""
    bare = tmp_path / "empty.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    ws = tmp_path / "ws"

    r = await repo_base.sync_workspace(ws, bare.as_uri(), "main")
    assert r.status == "remote_unavailable" and not r.fatal
    assert not ws.is_dir() or list(ws.iterdir()) == []
