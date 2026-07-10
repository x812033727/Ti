"""任務邊界部署漂移自查（完成率第三輪修法二A）。

背景：autodeploy timer 只在「無進行中討論」時 pull+restart，autopilot 連續跑任務時討論
幾乎總在進行 → 部署窗口極少，已合併修法長時間「紙上上線」（實測 #369/#370 合併後數小時
進不了執行碼）；execv 自我重載又要磁碟碼先變才觸發（雞生蛋）。`_maybe_boundary_redeploy`
掛在主迴圈任務邊界（保證無 autopilot 討論）自查 drift 並就地重佈。

覆蓋：drift＋idle→redeploy＋execv 準備；busy→略過；無 drift→略過；節流；fetch 失敗容錯；
redeploy 失敗→退避＋回填修復任務、不 pause、不 execv；旋鈕 0＝關閉。
全程 stub deploy/history/os.execv，不打網路、不真部署。
"""

from __future__ import annotations

import pytest

from studio import autopilot, backlog, config, deploy


class DeployStub:
    """攔截 deploy 模組：可控的 fetch/rev-parse 結果與 redeploy 結局。"""

    def __init__(self, *, disk="aaa111", origin="bbb222", fetch_rc=0, redeploy_ok=True):
        self.disk = disk
        self.origin = origin
        self.fetch_rc = fetch_rc
        self.redeploy_ok = redeploy_ok
        self.redeploy_calls = 0

    async def run(self, cmd, cwd=None, timeout=600):
        joined = " ".join(cmd)
        if "fetch" in joined:
            return (self.fetch_rc, "" if self.fetch_rc == 0 else "network down")
        if "rev-parse" in joined:
            return (0, self.origin + "\n")
        return (0, "")

    async def current_head(self, repo_dir):
        return self.disk

    async def redeploy(self):
        self.redeploy_calls += 1
        return (self.redeploy_ok, "ok" if self.redeploy_ok else "健康檢查失敗→回滾成功")


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_STATE_DIR", tmp_path / "ap")
    monkeypatch.setattr(config, "AUTOPILOT_DRYRUN", False)
    monkeypatch.setattr(config, "AUTOPILOT_DEPLOY_CHECK_INTERVAL", 300)
    monkeypatch.setattr(config, "AUTOPILOT_DEPLOY_FAIL_BACKOFF", 1800)
    # 重設模組級節流/退避（行程記憶體）
    monkeypatch.setattr(autopilot, "_last_deploy_check_at", 0.0)
    monkeypatch.setattr(autopilot, "_deploy_backoff_until", 0.0)
    return tmp_path


def _install(monkeypatch, stub: DeployStub, *, busy=False):
    monkeypatch.setattr(deploy, "_run", stub.run)
    monkeypatch.setattr(deploy, "current_head", stub.current_head)
    monkeypatch.setattr(deploy, "redeploy", stub.redeploy)

    import studio.history as history_mod

    monkeypatch.setattr(
        history_mod, "busy_sessions", lambda *_a, **_k: [{"session_id": "m1"}] if busy else []
    )

    execs: list = []

    async def _fake_prepare():
        execs.append("prepare")

    monkeypatch.setattr(autopilot, "_prepare_execv_reload", _fake_prepare)
    import os as os_mod

    monkeypatch.setattr(os_mod, "execv", lambda *a: execs.append("execv"))
    # 讓「自身碼有變」判定可控：pre_sig 與 post 不同 → 觸發 execv
    sigs = iter([1.0, 2.0, 2.0, 2.0])
    monkeypatch.setattr(autopilot, "_self_sig", lambda: next(sigs, 2.0))
    return execs


@pytest.mark.asyncio
async def test_drift_and_idle_triggers_redeploy_and_execv(state, monkeypatch):
    stub = DeployStub()
    execs = _install(monkeypatch, stub)

    await autopilot._maybe_boundary_redeploy()

    assert stub.redeploy_calls == 1
    assert execs == ["prepare", "execv"], "重佈成功且自身碼有變須走 execv 重載序列"


@pytest.mark.asyncio
async def test_busy_manual_discussion_defers_to_timer(state, monkeypatch):
    stub = DeployStub()
    _install(monkeypatch, stub, busy=True)

    await autopilot._maybe_boundary_redeploy()

    assert stub.redeploy_calls == 0, "有進行中討論不得重佈（交還 autodeploy timer）"


@pytest.mark.asyncio
async def test_no_drift_skips(state, monkeypatch):
    stub = DeployStub(disk="same999", origin="same999")
    execs = _install(monkeypatch, stub)

    await autopilot._maybe_boundary_redeploy()

    assert stub.redeploy_calls == 0
    assert execs == []


@pytest.mark.asyncio
async def test_throttle_interval(state, monkeypatch):
    stub = DeployStub(disk="same999", origin="same999")
    _install(monkeypatch, stub)

    calls = {"n": 0}
    orig = stub.run

    async def counting_run(cmd, cwd=None, timeout=600, **kwargs):
        calls["n"] += 1
        return await orig(cmd, cwd=cwd, timeout=timeout, **kwargs)

    monkeypatch.setattr(deploy, "_run", counting_run)
    await autopilot._maybe_boundary_redeploy()
    first = calls["n"]
    assert first > 0
    await autopilot._maybe_boundary_redeploy()  # 節流間隔內：不再打 git
    assert calls["n"] == first, "節流間隔內不得重複檢查"


@pytest.mark.asyncio
async def test_fetch_failure_is_tolerated(state, monkeypatch):
    stub = DeployStub(fetch_rc=1)
    execs = _install(monkeypatch, stub)

    await autopilot._maybe_boundary_redeploy()  # 不拋即通過

    assert stub.redeploy_calls == 0
    assert execs == []


@pytest.mark.asyncio
async def test_redeploy_failure_backs_off_and_files_fix_task(state, monkeypatch):
    stub = DeployStub(redeploy_ok=False)
    execs = _install(monkeypatch, stub)

    paused = []
    monkeypatch.setattr(autopilot, "_pause", lambda *a, **k: paused.append(a))

    await autopilot._maybe_boundary_redeploy()

    assert stub.redeploy_calls == 1
    assert "execv" not in execs, "重佈失敗不得 execv"
    assert not paused, "邊界重佈失敗不得 _pause（壞 commit 非本任務產物）"
    titles = [t["title"] for t in backlog.list_tasks()]
    assert "修復導致重佈失敗的 regression" in titles, "須回填修復任務"
    assert autopilot._deploy_backoff_until > 0, "須進入退避"
    # 退避期間：即使節流重設，也不再重佈
    monkeypatch.setattr(autopilot, "_last_deploy_check_at", 0.0)
    await autopilot._maybe_boundary_redeploy()
    assert stub.redeploy_calls == 1, "退避期間不得重試重佈"


@pytest.mark.asyncio
async def test_kill_switch_disables(state, monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_DEPLOY_CHECK_INTERVAL", 0)
    stub = DeployStub()
    _install(monkeypatch, stub)

    await autopilot._maybe_boundary_redeploy()

    assert stub.redeploy_calls == 0
