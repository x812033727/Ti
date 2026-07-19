"""專家閒置回收 reaper(效能強化 B1,預設關)+ _get_critic 覆蓋 bug 修復回歸。

守護不變量:
- TTL=0(預設)完全不起 reaper(零行為改變 oracle)。
- 閒置逾 TTL 的專家被 release(斷線+重建 client);豁免角色(pm)不回收;in-flight 不回收。
- release 後再 speak 自動重連(start() 冪等)。
- _get_critic:同 lane 兩種視角各自有 critic(舊寫法整 dict 覆蓋,第二種視角永遠 None
  靜默放行——critic gate 形同虛設)。
"""

from __future__ import annotations

import asyncio
import time

import pytest

from studio import config, experts
from studio.roles import BY_KEY


class FakeClient:
    def __init__(self):
        self.connected = False
        self.disconnects = 0

    async def connect(self):
        self.connected = True

    async def disconnect(self):
        self.disconnects += 1
        self.connected = False


@pytest.fixture
def expert(monkeypatch, tmp_path):
    clients: list[FakeClient] = []

    def _fake_build(role, sid, cwd, model=""):
        c = FakeClient()
        clients.append(c)
        return c

    monkeypatch.setattr(experts, "_build_client", _fake_build)
    ex = experts.Expert(BY_KEY["security"], "sid", tmp_path)
    return ex, clients


@pytest.mark.asyncio
async def test_release_disconnects_and_rebuilds(expert):
    ex, clients = expert
    await ex.start()
    assert clients[0].connected
    ex._last_used = time.monotonic() - 1000

    await ex.release()

    assert clients[0].disconnects == 1, "release 須斷線(回收 SDK 子行程)"
    assert len(clients) == 2, "release 須重建 client"
    await ex.start()
    assert clients[1].connected, "release 後可重連(start 冪等)"


@pytest.mark.asyncio
async def test_release_noop_when_in_flight(expert):
    ex, clients = expert
    await ex.start()
    ex._in_flight = True
    await ex.release()
    assert clients[0].disconnects == 0, "發言中絕不回收"
    assert ex.idle_for() == 0.0, "in-flight 時 idle_for 恆 0"


@pytest.mark.asyncio
async def test_reaper_respects_ttl_exempt_and_inflight(monkeypatch, tmp_path):
    from studio.orchestrator import StudioSession

    released: list[str] = []

    class FakeExpert:
        def __init__(self, key, idle):
            self.role = BY_KEY[key]
            self._idle = idle

        def idle_for(self):
            return self._idle

        async def release(self):
            released.append(self.role.key)

        async def speak(self, *a):
            return ""

        async def stop(self):
            pass

    monkeypatch.setattr(config, "EXPERT_IDLE_STOP_S", 900)
    monkeypatch.setattr(config, "EXPERT_IDLE_STOP_EXEMPT", frozenset({"pm"}))

    async def bc(ev):
        pass

    session = StudioSession(
        "t",
        bc,
        experts={
            "pm": FakeExpert("pm", 5000),  # 豁免
            "senior": FakeExpert("senior", 5000),  # 應回收
            "security": FakeExpert("security", 100),  # 未逾 TTL
        },
        cwd=None,
    )

    async def fast_sleep(_s):
        if released:  # 掃過一輪即結束
            raise asyncio.CancelledError()

    monkeypatch.setattr(experts.asyncio, "sleep", fast_sleep, raising=False)
    import studio.orchestrator as orch_mod

    monkeypatch.setattr(orch_mod.asyncio, "sleep", fast_sleep)

    with pytest.raises(asyncio.CancelledError):
        await session._idle_reaper()

    assert released == ["senior"], f"只回收逾 TTL 且非豁免者:{released}"


@pytest.mark.asyncio
async def test_reaper_not_started_when_ttl_zero(monkeypatch):
    """TTL=0(預設)不起 reaper——零行為改變 oracle。"""
    from studio.orchestrator import StudioSession

    monkeypatch.setattr(config, "EXPERT_IDLE_STOP_S", 0)
    created: list = []
    orig_create = asyncio.create_task

    def spy_create(coro, **kw):
        created.append(getattr(coro, "__name__", str(coro)))
        return orig_create(coro, **kw)

    async def bc(ev):
        pass

    session = StudioSession("t", bc, experts={}, cwd=None)

    async def _noop(requirement):
        return {}

    session._run = _noop
    monkeypatch.setattr(asyncio, "create_task", spy_create)
    await session.run("需求")
    assert not any("_idle_reaper" in str(n) for n in created), "TTL=0 不得建立 reaper task"


# --- _get_critic 覆蓋 bug 回歸 -------------------------------------------------


@pytest.mark.asyncio
async def test_get_critic_supports_multiple_viewpoints(monkeypatch, tmp_path):
    from studio.orchestrator import LaneContext, StudioSession

    made: list[str] = []

    class FakeCritic:
        def __init__(self, key):
            self.key = key

    def fake_make_expert(role, sid, cwd, **kw):
        made.append(role.key)
        return FakeCritic(role.key)

    import studio.providers as providers_mod

    monkeypatch.setattr(providers_mod, "make_expert", fake_make_expert)
    monkeypatch.setattr(config, "OFFLINE_MODE", False)

    async def bc(ev):
        pass

    session = StudioSession("t", bc, experts={}, cwd=tmp_path)
    ctx = LaneContext("main", tmp_path, {})

    c1 = session._get_critic(ctx, "senior")
    c2 = session._get_critic(ctx, "security")
    assert c1 is not None and c1.key == "senior"
    assert (
        c2 is not None and c2.key == "security"
    ), "第二種視角不得因 dict 被整個覆蓋而拿到 None(靜默放行=critic gate 形同虛設)"
    assert session._get_critic(ctx, "senior") is c1, "同視角複用既有實例"
    assert made == ["senior", "security"], "各視角只建一次"
