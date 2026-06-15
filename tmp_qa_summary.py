"""QA 摘要工具：把 spec rg 命中套用豁免後按檔案分組。"""
import subprocess
import re
from collections import Counter

r = subprocess.run(
    [
        "rg", "-n", "--glob", "!*.lock",
        "--glob", "!.git/**", "--glob", "!.venv/**",
        "--glob", "!.qa-venv/**", "--glob", "!.qa_venv/**",
        "--glob", "!.pc-cache-qa/**", "--glob", "!**/__pycache__/**",
        r"(^|[\s\`$])python\s", ".",
    ],
    cwd="/opt/ti-autopilot-work",
    capture_output=True, text=True,
)

# Path exempt (substring match, simple)
PATH_EXEMPT_SUBSTR = [
    "DECISIONS.md", "adr.json", "NOTES.md",
    "BASELINE_task", "CLOSURE_task", "docs/issues/",
    "studio/docs/dev_command_dedup_inventory.md",
    "studio/docs/subprocess_migration_inventory.md",
    "tests/docs/test_qa_task2_no_bare_python.py",
    "tests/docs/test_qa_task2_contributing_canonical.py",
    "tests/docs/test_qa_task2_happy_path.py",
    "tests/docs/test_qa_task2_merge_admin_prereq_doc.py",
    "tests/docs/test_qa_task3_precommit_step.py",
    "tests/docs/test_qa_task3_readme_test_section.py",
    "tests/docs/test_docs_pytest_command.py",
    "tests/docs/test_readme_consistency.py",
    "tests/docs/test_readme_verify_cmd.py",
]

# Line exempt (compiled)
LINE_EXEMPT = [
    re.compile(r"\.venv/bin/python($|[\s)`'\"\\,;\\-])"),
    re.compile(r"\.venv.Scripts.python"),
    re.compile(r"^#!.*\bpython\b"),
    re.compile(r"FROM\s+\S*python"),
    re.compile(r"^[a-zA-Z_][\w.-]*\s*=\s*['\"]?python[\w.-]*['\"]?$"),
    re.compile(r"\bpython[\w]*-[a-zA-Z][\w-]*"),
    re.compile(r"\bpython[\w]*[_-][a-zA-Z][\w-]*\b"),
    re.compile(r"\bpython3\.\d+\b"),
]

files = Counter()
violations_by_file = {}
for ln in r.stdout.splitlines():
    parts = ln.split(":", 2)
    if len(parts) < 3:
        continue
    rel, lineno_s, content = parts
    # 套 path 豁免
    if any(e in rel for e in PATH_EXEMPT_SUBSTR):
        continue
    # 套 line 豁免
    if any(pat.search(content) for pat in LINE_EXEMPT):
        continue
    files[rel] += 1
    violations_by_file.setdefault(rel, []).append((lineno_s, content))

total = sum(files.values())
print(f"豁免後違規: {total} hits in {len(files)} files\n")
for f, n in sorted(files.items(), key=lambda x: -x[1]):
    print(f"  {n:>3}  {f}")
print()
print("=" * 60)
print("完整違規清單（給工程師定位）：")
print("=" * 60)
for f, items in sorted(violations_by_file.items()):
    print(f"\n--- {f} ({len(items)}) ---")
    for lineno, content in items:
        print(f"  L{lineno}: {content}")
