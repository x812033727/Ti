"""近期 merged 標題語料與疑似已實作 prefilter helper。

涵蓋 #1 的資料取得與純比對，以及 #3 的黑白樣本邊界。
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime, timedelta

import pytest

from studio import autopilot, config


@pytest.fixture(autouse=True)
def clean_prefilter_state():
    old = {
        "AUTOPILOT_PREFILTER_IMPLEMENTED": config.AUTOPILOT_PREFILTER_IMPLEMENTED,
        "AUTOPILOT_PREFILTER_RATIO": config.AUTOPILOT_PREFILTER_RATIO,
        "AUTOPILOT_PREFILTER_LOOKBACK_DAYS": config.AUTOPILOT_PREFILTER_LOOKBACK_DAYS,
        "GITHUB_TOKEN": config.GITHUB_TOKEN,
    }
    autopilot._MERGED_TITLE_CACHE.clear()
    yield
    autopilot._MERGED_TITLE_CACHE.clear()
    for name, value in old.items():
        setattr(config, name, value)


def test_prefilter_config_knobs_reload(monkeypatch):
    monkeypatch.setenv("TI_AUTOPILOT_PREFILTER_IMPLEMENTED", "0")
    monkeypatch.setenv("TI_AUTOPILOT_PREFILTER_RATIO", "0.91")
    monkeypatch.setenv("TI_AUTOPILOT_PREFILTER_LOOKBACK_DAYS", "14")

    config.reload()

    assert config.AUTOPILOT_PREFILTER_IMPLEMENTED is False
    assert config.AUTOPILOT_PREFILTER_RATIO == pytest.approx(0.91)
    assert config.AUTOPILOT_PREFILTER_LOOKBACK_DAYS == 14


@pytest.mark.asyncio
async def test_prefilter_miss_leaves_task_unchanged(tmp_path, monkeypatch):
    task = {
        "title": "Add merged title prefilter",
        "lane": "qa",
        "note": "seed",
    }
    before = dict(task)

    async def _fake_corpus(clone, repo=None):
        return ["Completely different merged title"]

    monkeypatch.setattr(autopilot, "_recent_merged_title_corpus", _fake_corpus)

    assert await autopilot._prefilter_implemented_match(task, str(tmp_path)) is None
    assert task == before


@pytest.mark.asyncio
async def test_prefilter_short_title_bypasses_corpus_fetch(tmp_path, monkeypatch):
    async def _boom(clone, repo=None):
        raise AssertionError("短標題不應進入 merged title 語料查詢")

    monkeypatch.setattr(autopilot, "_recent_merged_title_corpus", _boom)

    assert (
        await autopilot._prefilter_implemented_match({"title": "fix tests"}, str(tmp_path)) is None
    )


@pytest.mark.asyncio
async def test_prefilter_disabled_bypasses_corpus_fetch(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AUTOPILOT_PREFILTER_IMPLEMENTED", False)

    async def _boom(clone, repo=None):
        raise AssertionError("總開關關閉時不應查詢 merged title 語料")

    monkeypatch.setattr(autopilot, "_recent_merged_title_corpus", _boom)

    assert (
        await autopilot._prefilter_implemented_match(
            {"title": "Add merged title prefilter"},
            str(tmp_path),
        )
        is None
    )


def test_first_similar_implemented_title_uses_token_set_and_skips_short_titles():
    merged = ["adds retry timeout guard", "fix tests"]

    assert (
        autopilot._first_similar_implemented_title(
            "add retry timeout guard",
            merged,
            threshold=0.80,
        )
        == "adds retry timeout guard"
    )
    assert (
        autopilot._first_similar_implemented_title(
            "fix tests",
            ["fix tests now"],
            threshold=0.10,
        )
        is None
    )
    assert (
        autopilot._first_similar_implemented_title(
            "add retry timeout guard",
            ["fix tests"],
            threshold=0.10,
        )
        is None
    )


def test_git_log_parser_skips_github_merge_subject():
    out = (
        "Merge pull request #42 from owner/topic\n\nAdd merged title prefilter\n\x00"
        "Fix direct commit\n\nBody line\n\x00"
    )

    assert autopilot._extract_git_log_titles(out) == [
        "Add merged title prefilter",
        "Fix direct commit",
    ]


@pytest.mark.asyncio
async def test_fetch_github_merged_titles_filters_recent_merged_prs(monkeypatch):
    import httpx

    recent = (datetime.now(UTC) - timedelta(days=2)).isoformat().replace("+00:00", "Z")
    old = (datetime.now(UTC) - timedelta(days=90)).isoformat().replace("+00:00", "Z")

    class _Resp:
        status_code = 200

        def json(self):
            return [
                {"title": "Recent merged prefilter", "merged_at": recent},
                {"title": "Old merged prefilter", "merged_at": old},
                {"title": "Closed but unmerged", "merged_at": None},
            ]

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url, headers, params):
            assert url == "https://api.github.com/repos/owner/repo/pulls"
            assert headers["Authorization"] == "Bearer tok"
            assert params["state"] == "closed"
            return _Resp()

    monkeypatch.setattr(config, "GITHUB_TOKEN", "tok")
    monkeypatch.setattr(httpx, "AsyncClient", _Client)

    assert await autopilot._fetch_github_merged_titles("owner/repo", 60) == [
        "Recent merged prefilter"
    ]


@pytest.mark.asyncio
async def test_fetch_merged_titles_falls_back_to_git_log_without_token(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args: str) -> None:
        subprocess.run(
            ["git", *args],
            cwd=repo,
            check=True,
            text=True,
            capture_output=True,
            timeout=10,
        )

    git("init", "-q")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test")
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    git("add", "a.txt")
    git(
        "commit",
        "-q",
        "-m",
        "Merge pull request #1 from owner/topic",
        "-m",
        "Add offline merged title corpus",
    )

    monkeypatch.setattr(config, "GITHUB_TOKEN", "")

    assert await autopilot._fetch_merged_titles(str(repo), "owner/repo", 60) == [
        "Add offline merged title corpus"
    ]
