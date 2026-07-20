"""Source identity lane must not be confused with fast/full workflow routing."""

import inspect

from studio import autonomy, autopilot, improver


def test_managed_baselines_use_stable_source_workspace_lane():
    assert autonomy.SOURCE_WORKSPACE_LANE == "main"
    core_source = inspect.getsource(autopilot.run_one_task)
    project_source = inspect.getsource(improver.ProjectImprover._run_task)
    assert '"lane": autonomy.SOURCE_WORKSPACE_LANE' in core_source
    assert '"lane": autonomy.SOURCE_WORKSPACE_LANE' in project_source


def test_workflow_lane_remains_separate_audit_dimension():
    source = inspect.getsource(autopilot.run_one_task)
    assert '"lane": "fast" if use_fast else "full"' in source
