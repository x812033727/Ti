"""任務 lane 接線：把 `禁改:` 清單帶入 commit，違規時廣播並回饋下一輪。"""

from __future__ import annotations

from studio import config, events, runner
from studio.orchestrator import LaneContext, StudioSession
from studio.roles import BY_KEY, Role
from studio.workflow import fast_track_workflow


class ScriptedExpert:
    def __init__(self, role: Role, scripts: list[str]):
        self.role = role
        self._scripts = scripts
        self.calls = 0
        self.prompts: list[str] = []

    async def speak(self, prompt: str, broadcast) -> str:
        self.prompts.append(prompt)
        text = self._scripts[min(self.calls, len(self._scripts) - 1)]
        self.calls += 1
        await broadcast(
            events.expert_message("t", self.role.key, self.role.name, self.role.avatar, text)
        )
        return text

    async def stop(self) -> None:
        pass


async def test_task_lane_forbidden_paths_block_commit_broadcast_and_feed_next_round(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(config, "TASK_MAX_ROUNDS", 2)
    monkeypatch.setattr(config, "REFLEXION_ENABLED", False)
    monkeypatch.setattr(config, "HUDDLE_ENABLED", False)

    bucket: list[events.StudioEvent] = []

    async def broadcast(ev):
        bucket.append(ev)

    experts = {
        "engineer": ScriptedExpert(BY_KEY["engineer"], ["第一輪改到 docs", "第二輪避開 docs"]),
        "qa": ScriptedExpert(BY_KEY["qa"], ["驗證: PASS"]),
    }
    session = StudioSession(
        "t", broadcast, experts=experts, cwd=tmp_path, workflow=fast_track_workflow()
    )
    ctx = LaneContext("main", tmp_path, experts, None)
    calls: list[dict] = []

    async def fake_git_commit(cwd, message, forbidden_paths=None):
        calls.append({"cwd": cwd, "message": message, "forbidden_paths": forbidden_paths})
        if len(calls) == 1:
            return runner.GitCommitResult(None, ["docs/protected.md"])
        return runner.GitCommitResult("abc123", [])

    monkeypatch.setattr(runner, "git_commit", fake_git_commit)

    task = {"id": 1, "title": "實作但不得碰 docs", "status": "todo", "forbidden_paths": ["docs/"]}
    ok = await session._work_task(ctx, task, "計畫")

    assert ok is True
    assert calls[0]["forbidden_paths"] == ["docs/"]
    assert calls[1]["forbidden_paths"] == ["docs/"]
    assert ctx.last_commit == "abc123"
    assert "禁改路徑違規" in experts["engineer"].prompts[1]
    assert "docs/protected.md" in experts["engineer"].prompts[1]

    violation_events = [
        e
        for e in bucket
        if e.type is events.EventType.RUN_RESULT
        and e.payload.get("forbidden_violations") == ["docs/protected.md"]
    ]
    assert violation_events
