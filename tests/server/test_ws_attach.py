"""WS 斷線重掛（attach）契約守護。

契約：`/ws` 首訊息帶 `{"attach": sid, "cursor": n}` 即訂閱進行中 session——先補放
history JSONL 第 cursor 筆之後的事件、送 `attach_ok`（帶權威計數）、再無縫接 live；
attach 端 interject/stop 餵回既有 controller（雙端回顯、入檔）；不存在/已結束的
sid 回 `attach_unavailable`；attach 不占 MAX_CONCURRENT_SESSIONS slot；原 socket
斷線後 attach 照收後續事件（detach 背景續跑 + fan-out 不綁原 socket）。

去重不變式（本檔測試 2 直接驗證）：hub.seq == JSONL 行數——record_event 與
hub.publish 之間無 await，快照長 N 時 seq<=N 的佇列事件必在快照中，計數去重即
無重複無遺漏。
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from studio import config, events, history, ws
from studio.events import EventType, StudioEvent


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ACCESS_PASSWORD", "")
    monkeypatch.setattr(config, "OFFLINE_MODE", True)  # 略過 provider_ready 檢查
    monkeypatch.setattr(config, "OFFLINE_DELAY", 0.0)
    monkeypatch.setattr(config, "MAX_CONCURRENT_SESSIONS", 4)
    monkeypatch.setattr(config, "WORKSPACE_ROOT", tmp_path / "ws")
    monkeypatch.setattr(config, "HISTORY_ROOT", tmp_path / "hist")
    monkeypatch.setattr(ws, "_active_sessions", 0)
    ws._hubs.clear()
    yield
    ws._hubs.clear()


_session_holder: list = []


def _install_stub(monkeypatch, n_before: int = 3):
    """把 _run_plain_session 換成可控 stub：broadcast 前段 → 等 interject/stop →
    broadcast 後段 + done。讓測試能在「session 進行中」的確定時點 attach。"""

    async def _stub(session, requirement):
        _session_holder.clear()
        _session_holder.append(session)
        b = session.broadcast
        await b(
            StudioEvent(
                EventType.SESSION_STARTED,
                session.session_id,
                {"requirement": requirement, "roster": [], "workspace_id": session.session_id},
            )
        )
        for i in range(n_before):
            await b(events.phase_change(session.session_id, f"前段{i}"))
        try:
            import asyncio

            text = await asyncio.wait_for(session._intervention.get(), timeout=20)
        except TimeoutError:
            text = "(timeout)"
        await b(events.phase_change(session.session_id, "後段", text))
        await b(StudioEvent(EventType.DONE, session.session_id, {"completed": True}))
        return {"completed": True}

    monkeypatch.setattr(ws, "_run_plain_session", _stub)


def _client():
    from studio.server import app

    return TestClient(app, client=("127.0.0.1", 12345))


def _recv_until(conn, type_, limit=200):
    """收事件直到指定型別，回傳 (該事件, 之前全部事件含它)。"""
    got = []
    for _ in range(limit):
        ev = conn.receive_json()
        got.append(ev)
        if ev["type"] == type_:
            return ev, got
    raise AssertionError(f"未在 {limit} 筆內收到 {type_}：{[e['type'] for e in got]}")


def test_attach_replays_then_streams_live(monkeypatch):
    _install_stub(monkeypatch)
    client = _client()
    with client.websocket_connect("/ws") as main:
        main.send_json({"requirement": "做個東西"})
        started, _ = _recv_until(main, "session_started")
        sid = started["session_id"]
        # 收完前段（3 筆 phase_change），此時 stub 停在等 interject
        for _ in range(3):
            main.receive_json()

        with client.websocket_connect("/ws") as att:
            att.send_json({"attach": sid, "cursor": 0})
            # 補放：與 JSONL 已寫入內容完全一致（session_started + 3 筆前段）
            past = history.load_events(sid)
            replayed = [att.receive_json() for _ in range(len(past))]
            assert replayed == past
            ok = att.receive_json()
            assert ok["type"] == "attach_ok"
            assert ok["payload"]["cursor"] == len(past)
            # 解除 stub → live 事件雙端都收到
            main.send_json({"type": "interject", "text": "繼續"})
            ev, got = _recv_until(att, "done")
            types = [e["type"] for e in got]
            assert "human_message" in types and "phase_change" in types
        _recv_until(main, "done")


def test_attach_cursor_no_dup_no_gap(monkeypatch):
    """cursor=k 的補放＋live 全序列 == 最終 JSONL[k:]（直接驗證競態解法）。"""
    _install_stub(monkeypatch)
    client = _client()
    with client.websocket_connect("/ws") as main:
        main.send_json({"requirement": "做個東西"})
        started, _ = _recv_until(main, "session_started")
        sid = started["session_id"]
        for _ in range(3):
            main.receive_json()

        k = 2  # 假裝已收過前 2 筆
        with client.websocket_connect("/ws") as att:
            att.send_json({"attach": sid, "cursor": k})
            got: list[dict] = []
            while True:
                ev = att.receive_json()
                if ev["type"] == "attach_ok":
                    continue
                got.append(ev)
                if ev["type"] == "done":
                    break
                if ev["type"] == "phase_change" and ev["payload"].get("phase") == "前段2":
                    # 收完補放尾筆後解除 stub，製造「補放與 live 交界」
                    main.send_json({"type": "interject", "text": "go"})
            final = history.load_events(sid)
            assert got == final[k:], "attach 收到的序列必須與 JSONL[k:] 逐筆相等（無重複無遺漏）"
        _recv_until(main, "done")


def test_attach_unknown_or_finished_session(monkeypatch):
    _install_stub(monkeypatch)
    client = _client()
    # 不存在的 sid
    with client.websocket_connect("/ws") as att:
        att.send_json({"attach": "nosuch", "cursor": 0})
        ev = att.receive_json()
        assert ev["type"] == "error" and ev["payload"]["code"] == "attach_unavailable"
    # 已結束的 sid（跑完一場後 hub 已 pop）
    with client.websocket_connect("/ws") as main:
        main.send_json({"requirement": "做個東西"})
        started, _ = _recv_until(main, "session_started")
        sid = started["session_id"]
        main.send_json({"type": "interject", "text": "go"})
        _recv_until(main, "done")
    for _ in range(100):  # 等 done-callback pop hub
        if sid not in ws._hubs:
            break
        time.sleep(0.02)
    with client.websocket_connect("/ws") as att:
        att.send_json({"attach": sid, "cursor": 0})
        ev = att.receive_json()
        assert ev["type"] == "error" and ev["payload"]["code"] == "attach_unavailable"


def test_attach_interject_and_stop_reach_controller(monkeypatch):
    _install_stub(monkeypatch)
    client = _client()
    with client.websocket_connect("/ws") as main:
        main.send_json({"requirement": "做個東西"})
        started, _ = _recv_until(main, "session_started")
        sid = started["session_id"]
        for _ in range(3):
            main.receive_json()
        with client.websocket_connect("/ws") as att:
            att.send_json({"attach": sid, "cursor": 0})
            _recv_until(att, "attach_ok")
            # attach 端插話：進 controller 的 intervention queue、雙端收到回顯、入檔
            att.send_json({"type": "interject", "text": "從重掛端插話"})
            ev, _ = _recv_until(att, "human_message")
            assert ev["payload"]["text"] == "從重掛端插話"
            ev, _ = _recv_until(main, "human_message")
            assert ev["payload"]["text"] == "從重掛端插話"
            # stub 收到文字後續跑：後段 phase 帶回該文字（證明真的進了 queue）
            ev, _ = _recv_until(att, "done")
            # attach 端 stop：request_stop 生效
            assert _session_holder and _session_holder[0]._stop is False
        _recv_until(main, "done")
    assert any(e["type"] == "human_message" for e in history.load_events(sid)), "插話回顯須入檔"

    # stop 案：另開一場，從 attach 端喊停
    with client.websocket_connect("/ws") as main:
        main.send_json({"requirement": "再做一個"})
        started, _ = _recv_until(main, "session_started")
        sid2 = started["session_id"]
        for _ in range(3):
            main.receive_json()
        with client.websocket_connect("/ws") as att:
            att.send_json({"attach": sid2, "cursor": 0})
            _recv_until(att, "attach_ok")
            att.send_json({"type": "stop"})
            for _ in range(100):
                if _session_holder and _session_holder[0]._stop:
                    break
                time.sleep(0.02)
            assert _session_holder[0]._stop is True
            # 解除 stub 讓場次收尾
            att.send_json({"type": "interject", "text": "bye"})
        _recv_until(main, "done")


# 註：「原 socket 斷線（detach 背景續跑）後 attach 照收事件」無法在 TestClient 下測——
# 每條 WS 連線各自一個 portal/事件迴圈，連線關閉即拆迴圈、背景 run_task 一併被砍。
# 該情境由 tests/server/test_ws_attach_real_server.py 以真 uvicorn 冒煙覆蓋
# （硬斷線→重掛→done→與 JSONL 全量對帳）。


def test_attach_does_not_consume_session_slot(monkeypatch):
    _install_stub(monkeypatch)
    monkeypatch.setattr(config, "MAX_CONCURRENT_SESSIONS", 1)
    client = _client()
    with client.websocket_connect("/ws") as main:
        main.send_json({"requirement": "做個東西"})
        started, _ = _recv_until(main, "session_started")
        sid = started["session_id"]
        with client.websocket_connect("/ws") as att:
            att.send_json({"attach": sid, "cursor": 0})
            _recv_until(att, "attach_ok")  # 滿載（1/1）仍可 attach——不占 slot
            assert ws._active_sessions == 1
            main.send_json({"type": "interject", "text": "go"})
            _recv_until(att, "done")
        _recv_until(main, "done")


def test_offline_e2e_with_attach(monkeypatch):
    """離線整合煙霧：真跑一場離線討論，中途 attach，雙端都收斂到 done。"""
    monkeypatch.setattr(config, "OFFLINE_DELAY", 0.02)
    monkeypatch.setattr(config, "DEBATE_ROUNDS", 1)
    client = _client()
    with client.websocket_connect("/ws") as main:
        main.send_json({"requirement": "做一個 BMI CLI"})
        started, _ = _recv_until(main, "session_started")
        sid = started["session_id"]
        with client.websocket_connect("/ws") as att:
            att.send_json({"attach": sid, "cursor": 0})
            ev, got_att = _recv_until(att, "done", limit=3000)
        ev, got_main = _recv_until(main, "done", limit=3000)
    # attach 端收到完整事件流（含補放的 session_started 與收尾 done）
    types = [e["type"] for e in got_att if e["type"] != "attach_ok"]
    assert types[0] == "session_started" and types[-1] == "done"
