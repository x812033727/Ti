from __future__ import annotations

import shutil
import subprocess
import sys
import textwrap
import types

import pytest
from _repo import REPO_ROOT

from studio import events, experts
from studio.roles import BY_KEY


def test_expert_message_aborted_flag_is_opt_in():
    base = events.expert_message("s", "qa", "QA", "", "ok")
    falsey = events.expert_message("s", "qa", "QA", "", "ok", aborted=False)
    aborted = events.expert_message(
        "s",
        "qa",
        "QA",
        "",
        "timeout",
        duration_s=1.25,
        provider="fake",
        model="m",
        role="qa",
        aborted=True,
    )

    assert "aborted" not in base.payload
    assert "aborted" not in base.to_dict()["payload"]
    assert "aborted" not in falsey.payload
    assert aborted.payload["aborted"] is True
    assert aborted.to_dict()["payload"]["aborted"] is True
    assert aborted.payload["duration_s"] == 1.25


class _AbortClient:
    def __init__(self, result_message, *, fail_interrupt: bool = False):
        self.result_message = result_message
        self.fail_interrupt = fail_interrupt
        self.interrupts = 0
        self.disconnects = 0

    async def connect(self):
        pass

    async def disconnect(self):
        self.disconnects += 1

    async def interrupt(self):
        self.interrupts += 1
        if self.fail_interrupt:
            raise RuntimeError("wedged control channel")

    def receive_response(self):
        async def gen():
            yield self.result_message()

        return gen()


@pytest.fixture
def fake_claude_sdk(monkeypatch):
    mod = types.ModuleType("claude_agent_sdk")

    class ResultMessage:
        pass

    mod.ResultMessage = ResultMessage
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", mod)
    return mod


@pytest.mark.parametrize("fail_interrupt", [False, True])
async def test_claude_abort_turn_broadcasts_aborted_event_after_recovery(
    fake_claude_sdk, monkeypatch, tmp_path, fail_interrupt
):
    first = _AbortClient(fake_claude_sdk.ResultMessage, fail_interrupt=fail_interrupt)
    rebuilt = _AbortClient(fake_claude_sdk.ResultMessage)
    builds = iter([first, rebuilt])
    monkeypatch.setattr(experts, "_build_client", lambda _role, _sid, _cwd: next(builds))

    expert = experts.Expert(BY_KEY["engineer"], "sess", tmp_path)
    bucket: list[events.StudioEvent] = []

    async def broadcast(ev: events.StudioEvent) -> None:
        bucket.append(ev)

    note = await expert._abort_turn(experts.ExpertTurnTimeout("idle", "partial output"), broadcast)

    assert "\u767c\u8a00\u903e\u6642\u4e2d\u6b62" in note
    assert "partial output" in note
    assert first.interrupts == 1
    if fail_interrupt:
        assert first.disconnects == 1
        assert expert._client is rebuilt
        assert "\u5df2\u91cd\u5efa" in note
    else:
        assert first.disconnects == 0
        assert expert._client is first

    messages = [ev for ev in bucket if ev.type == events.EventType.EXPERT_MESSAGE]
    assert len(messages) == 1
    payload = messages[0].payload
    assert payload["speaker"] == "engineer"
    assert payload["aborted"] is True
    assert "\u903e\u6642" in payload["text"]
    assert "partial output" in payload["text"]


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_frontend_aborted_class_is_conditional():
    script = r"""
        const { pathToFileURL } = await import("node:url");

        function makeEl(tag = "") {
          const el = {
            tag,
            children: [],
            dataset: {},
            style: {},
            className: "",
            disabled: false,
            value: "",
            textContent: "",
            innerHTML: "",
            scrollTop: 0,
            scrollHeight: 0,
            classes: new Set(),
            classList: {
              add(...names) { for (const name of names) el.classes.add(name); },
              remove(...names) { for (const name of names) el.classes.delete(name); },
              toggle(name, force) {
                const on = force === undefined ? !el.classes.has(name) : Boolean(force);
                if (on) el.classes.add(name); else el.classes.delete(name);
                return on;
              },
              contains(name) { return el.classes.has(name); },
            },
            appendChild(child) { this.children.push(child); return child; },
            querySelector() { return makeEl("query"); },
            querySelectorAll() { return []; },
            setAttribute() {},
            remove() {},
            addEventListener() {},
          };
          return el;
        }

        const stream = makeEl("stream");
        const body = makeEl("body");
        const document = {
          body,
          querySelector(sel) { return sel === "#stream" ? stream : makeEl(sel); },
          querySelectorAll() { return []; },
          createElement(tag) { return makeEl(tag); },
          createElementNS(_ns, tag) { return makeEl(tag); },
          createTextNode(text) { const el = makeEl("text"); el.textContent = text; return el; },
          getElementById() { return makeEl("id"); },
        };
        const location = { protocol: "http:", host: "localhost", href: "" };
        function WebSocket() { return makeEl("ws"); }
        WebSocket.OPEN = 1;
        const fetchStub = () => Promise.resolve({
          ok: true,
          json: () => Promise.resolve({ files: [], sessions: [], events: [], tasks: [], fields: [] }),
        });
        Object.assign(globalThis, {
          document,
          location,
          WebSocket,
          fetch: fetchStub,
          window: {
            addEventListener() {},
            matchMedia() { return { matches: false, addEventListener() {}, removeEventListener() {} }; },
          },
          setTimeout() { return 0; },
          clearTimeout() {},
        });

        const mod = await import(pathToFileURL(process.cwd() + "/web/js/events-render.js"));
        mod.addMessage({ speaker: "qa", name: "QA", avatar: "", text: "normal" });
        mod.addMessage({ speaker: "qa", name: "QA", avatar: "", text: "false", aborted: false });
        mod.addMessage({ speaker: "qa", name: "QA", avatar: "", text: "aborted", aborted: true });

        const [normal, falsey, aborted] = stream.children;
        if (!normal || !falsey || !aborted) throw new Error("messages were not appended");
        if (normal.classList.contains("aborted")) throw new Error("normal message marked aborted");
        if (falsey.classList.contains("aborted")) throw new Error("aborted:false message marked aborted");
        if (!aborted.classList.contains("aborted")) throw new Error("aborted:true message missing class");
        console.log("frontend aborted class conditional: ok");
    """
    result = subprocess.run(
        ["node", "--input-type=module", "--eval", textwrap.dedent(script)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "frontend aborted class conditional: ok" in result.stdout
