from studio import runner


def _find_pair(args: list[str], flag: str, value: str) -> int:
    for idx in range(len(args) - 1):
        if args[idx : idx + 2] == [flag, value]:
            return idx
    raise AssertionError(f"missing argv pair: {[flag, value]!r} in {args!r}")


def _find_triplet(args: list[str], flag: str, source: str, target: str) -> int:
    for idx in range(len(args) - 2):
        if args[idx : idx + 3] == [flag, source, target]:
            return idx
    raise AssertionError(f"missing argv triplet: {[flag, source, target]!r} in {args!r}")


def test_qa_task2_tmp_checkout_ro_binds_project_after_tmpfs(monkeypatch, tmp_path):
    fake_tmp_root = tmp_path / "tmp"
    project_root = fake_tmp_root / "repo"
    writable_cwd = fake_tmp_root / "lane-cwd"
    project_root.mkdir(parents=True)
    writable_cwd.mkdir()
    monkeypatch.setattr(runner, "_BWRAP_TMP_ROOT", fake_tmp_root)
    monkeypatch.setattr(runner, "_PROJECT_ROOT", project_root)

    args = runner._bwrap_prefix(writable_cwd)

    tmpfs_idx = _find_pair(args, "--tmpfs", "/tmp")
    project_ro_idx = _find_triplet(args, "--ro-bind", str(project_root), str(project_root))
    cwd_bind_idx = _find_triplet(args, "--bind", str(writable_cwd), str(writable_cwd))

    assert tmpfs_idx < project_ro_idx < cwd_bind_idx


def test_qa_task2_non_tmp_checkout_does_not_add_project_ro_bind(monkeypatch, tmp_path):
    fake_tmp_root = tmp_path / "tmp"
    project_root = tmp_path / "repo-outside-fake-tmp"
    writable_cwd = fake_tmp_root / "lane-cwd"
    project_root.mkdir()
    writable_cwd.mkdir(parents=True)
    monkeypatch.setattr(runner, "_BWRAP_TMP_ROOT", fake_tmp_root)
    monkeypatch.setattr(runner, "_PROJECT_ROOT", project_root)

    args = runner._bwrap_prefix(writable_cwd)

    assert ["--ro-bind", str(project_root), str(project_root)] not in [
        args[idx : idx + 3] for idx in range(len(args) - 2)
    ]
    _find_triplet(args, "--bind", str(writable_cwd), str(writable_cwd))


def test_qa_task2_project_root_cwd_stays_writable(monkeypatch, tmp_path):
    fake_tmp_root = tmp_path / "tmp"
    project_root = fake_tmp_root / "repo"
    project_root.mkdir(parents=True)
    monkeypatch.setattr(runner, "_BWRAP_TMP_ROOT", fake_tmp_root)
    monkeypatch.setattr(runner, "_PROJECT_ROOT", project_root)

    args = runner._bwrap_prefix(project_root)

    assert ["--ro-bind", str(project_root), str(project_root)] not in [
        args[idx : idx + 3] for idx in range(len(args) - 2)
    ]
    _find_triplet(args, "--bind", str(project_root), str(project_root))
