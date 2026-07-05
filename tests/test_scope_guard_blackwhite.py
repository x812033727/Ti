from _scope_guard import find_repo_scope_violations

ALLOWED_GLOBS = ("allowed/*.py",)


def test_scope_guard_flags_added_python_outside_allowed_globs(scope_repo):
    scope_repo.write("studio/sneaky_added.py", "VALUE = 1\n")
    scope_repo.commit("add sneaky python")

    violations = find_repo_scope_violations(
        scope_repo.path,
        scope_repo.baseline,
        ALLOWED_GLOBS,
    )

    assert violations == ["studio/sneaky_added.py"]


def test_scope_guard_flags_modified_python_outside_allowed_globs(scope_repo):
    scope_repo.write("studio/sneaky_modified.py", "VALUE = 1\n")
    baseline = scope_repo.commit("seed tracked python")
    scope_repo.write("studio/sneaky_modified.py", "VALUE = 2\n")
    scope_repo.commit("modify tracked python")

    violations = find_repo_scope_violations(
        scope_repo.path,
        baseline,
        ALLOWED_GLOBS,
    )

    assert violations == ["studio/sneaky_modified.py"]


def test_scope_guard_allows_allowlisted_python_and_non_python_changes(scope_repo):
    scope_repo.write("allowed/safe.py", "VALUE = 1\n")
    scope_repo.write("docs/scope.md", "notes\n")
    scope_repo.commit("add allowed and non-python files")

    violations = find_repo_scope_violations(
        scope_repo.path,
        scope_repo.baseline,
        ALLOWED_GLOBS,
    )

    assert violations == []
