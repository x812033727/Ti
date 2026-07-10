from __future__ import annotations

import inspect
import re
import tomllib
from pathlib import Path

from studio import backlog

ROOT = Path(__file__).resolve().parents[2]

PREFILTER_ENV_NAMES = (
    "TI_AUTOPILOT_PREFILTER_IMPLEMENTED",
    "TI_AUTOPILOT_PREFILTER_RATIO",
    "TI_AUTOPILOT_PREFILTER_LOOKBACK_DAYS",
)


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_prefilter_config_knobs_are_documented_and_defined_in_both_config_paths():
    env_example = _read(".env.example")
    config_src = _read("studio/config.py")

    for env_name in PREFILTER_ENV_NAMES:
        assert re.search(rf"^# {env_name}=", env_example, re.MULTILINE), env_name
        assert env_name in config_src

    assert "investigation lane" in env_example
    assert "git log" in env_example
    assert "_token_set_similarity" in env_example

    expected_assignments = (
        'AUTOPILOT_PREFILTER_IMPLEMENTED = os.getenv("TI_AUTOPILOT_PREFILTER_IMPLEMENTED", "1")',
        'AUTOPILOT_PREFILTER_RATIO = _env_float("TI_AUTOPILOT_PREFILTER_RATIO", 0.80)',
        'AUTOPILOT_PREFILTER_LOOKBACK_DAYS = _env_int("TI_AUTOPILOT_PREFILTER_LOOKBACK_DAYS", 60)',
    )
    for assignment in expected_assignments:
        assert config_src.count(assignment) == 2, assignment

    assert "global AUTOPILOT_PREFILTER_IMPLEMENTED, AUTOPILOT_PREFILTER_RATIO" in config_src
    assert "global AUTOPILOT_PREFILTER_LOOKBACK_DAYS" in config_src


def test_prefilter_does_not_expand_task_type_or_dependency_surface():
    assert backlog.VALID_TYPES == ("feature", "bug", "improvement")

    signature = inspect.signature(backlog.annotate)
    assert "lane" in signature.parameters
    assert all(
        param.kind is not inspect.Parameter.VAR_KEYWORD for param in signature.parameters.values()
    )

    pyproject = tomllib.loads(_read("pyproject.toml"))
    deps = pyproject["project"]["dependencies"]
    dep_names = {re.split(r"[<>=!~\[\] ]", dep, maxsplit=1)[0].lower() for dep in deps}
    assert dep_names.isdisjoint({"rapidfuzz", "fuzzywuzzy", "pygithub"})

    product_src = "\n".join(
        _read(path)
        for path in (
            "studio/autopilot.py",
            "studio/backlog.py",
            "studio/config.py",
        )
    )
    assert not re.search(r"\b(?:from|import)\s+(?:rapidfuzz|fuzzywuzzy|github)\b", product_src)
