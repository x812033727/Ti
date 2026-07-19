"""`禁改:` marker 解析與 `check_forbidden_paths` 純函式（任務 #1）。

純驗證 flow.py 決策層，不碰 async / cwd / git：
- parse_tasks_with_deps 對 `禁改: #<id> <pattern>, ...` 在對應 task dict 產生 forbidden_paths。
- 舊輸入（無禁改行）forbidden_paths 為空清單，向後相容。
- check_forbidden_paths 三類比對語意：單檔精確、目錄 `/` 前綴、fnmatch glob；不命中回空。
- 懸空 task id 的禁改行安全丟棄。
"""

from __future__ import annotations

from studio.flow import check_forbidden_paths, parse_tasks_with_deps

# --- 解析：禁改行寫入對應 task ------------------------------------------------


def test_forbidden_paths_parsed_onto_task():
    text = "任務: #1 前端\n任務: #2 設定\n禁改: #2 studio/config.py, docs/"
    tasks, _ = parse_tasks_with_deps(text)
    by_id = {t["id"]: t for t in tasks}
    assert by_id[2]["forbidden_paths"] == ["studio/config.py", "docs/"]
    assert by_id[1]["forbidden_paths"] == []  # 未宣告禁改的任務為空


def test_no_forbidden_line_backward_compat():
    tasks, _ = parse_tasks_with_deps("任務: #1 甲\n任務: #2 乙")
    assert all(t["forbidden_paths"] == [] for t in tasks)


def test_bullet_task_fallback_gets_empty_forbidden_paths():
    tasks, _ = parse_tasks_with_deps("- 甲\n- 乙")
    assert [t["title"] for t in tasks] == ["甲", "乙"]
    assert all(t["forbidden_paths"] == [] for t in tasks)


def test_forbidden_paths_dedup_preserves_order():
    text = "任務: #1 甲\n禁改: #1 a.py, docs/, a.py"
    tasks, _ = parse_tasks_with_deps(text)
    assert tasks[0]["forbidden_paths"] == ["a.py", "docs/"]


def test_dangling_forbidden_id_dropped():
    # #9 不存在 → 禁改行安全丟棄，不報錯、不誤掛到別的任務。
    text = "任務: #1 甲\n禁改: #9 secret.py"
    tasks, _ = parse_tasks_with_deps(text)
    assert tasks[0]["forbidden_paths"] == []


def test_fullwidth_colon_forbidden_line():
    text = "任務: #1 甲\n禁改：#1 studio/config.py"
    tasks, _ = parse_tasks_with_deps(text)
    assert tasks[0]["forbidden_paths"] == ["studio/config.py"]


# --- check_forbidden_paths：三類比對語意 -------------------------------------


def test_exact_file_hit():
    staged = ["studio/config.py", "studio/flow.py"]
    assert check_forbidden_paths(staged, ["studio/config.py"]) == ["studio/config.py"]


def test_directory_prefix_hit():
    staged = ["docs/a.md", "docs/sub/b.md", "studio/flow.py"]
    assert check_forbidden_paths(staged, ["docs/"]) == ["docs/a.md", "docs/sub/b.md"]


def test_directory_prefix_does_not_match_sibling_prefix():
    staged = ["docs-old/a.md", "docs", "docs/a.md"]
    assert check_forbidden_paths(staged, ["docs/"]) == ["docs/a.md"]


def test_fnmatch_glob_hit():
    staged = ["a.py", "src/x.js", "src/y.js", "readme.md"]
    assert check_forbidden_paths(staged, ["*.py"]) == ["a.py"]
    assert check_forbidden_paths(staged, ["src/*.js"]) == ["src/x.js", "src/y.js"]


def test_no_hit_returns_empty():
    staged = ["studio/flow.py", "README.md"]
    assert check_forbidden_paths(staged, ["studio/config.py", "docs/"]) == []


def test_empty_patterns_returns_empty():
    assert check_forbidden_paths(["a.py"], []) == []
    assert check_forbidden_paths(["a.py"], ["  ", ""]) == []


def test_multiple_patterns_dedup_violations():
    # 一檔被多 pattern 命中只回報一次；違規保序去重。
    staged = ["studio/config.py", "docs/a.md"]
    got = check_forbidden_paths(staged, ["studio/config.py", "*.py", "docs/"])
    assert got == ["studio/config.py", "docs/a.md"]


def test_backslash_path_normalized():
    assert check_forbidden_paths(["docs\\a.md"], ["docs/"]) == ["docs/a.md"]


def test_check_forbidden_paths_does_not_mutate_inputs():
    staged = ["docs\\a.md", "studio/config.py"]
    patterns = ["docs/", "studio/config.py"]

    assert check_forbidden_paths(staged, patterns) == ["docs/a.md", "studio/config.py"]
    assert staged == ["docs\\a.md", "studio/config.py"]
    assert patterns == ["docs/", "studio/config.py"]
