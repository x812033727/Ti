"""Live QA gate for task #2 PR descriptions.

This is intentionally outside pytest CI: it checks the current branch's real GitHub PR.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys


def _section(body: str, heading: str) -> str:
    match = re.search(rf"^## {re.escape(heading)}\n(?P<section>.*?)(?=^## |\Z)", body, re.M | re.S)
    return match.group("section").strip() if match else ""


def _run_gh_pr_view() -> tuple[int, str, str]:
    proc = subprocess.run(
        ["gh", "pr", "view", "--json", "number,title,body,url,headRefName,baseRefName"],
        check=False,
        text=True,
        capture_output=True,
        timeout=60,
    )
    return proc.returncode, proc.stdout, proc.stderr


def check_body(body: str) -> list[str]:
    problems: list[str] = []
    motivation = _section(body, "動機")
    verification = _section(body, "如何驗證")

    if not motivation:
        problems.append("缺少 `## 動機` 區塊")
    elif not any(
        token in motivation for token in ("bug", "邊界", "錯字", "過時", "未處理", "錯誤", "修正")
    ):
        problems.append("`## 動機` 未說明 bug／邊界／錯字／過時等具體原因")

    if not verification:
        problems.append("缺少 `## 如何驗證` 區塊")
    elif "對應測試：" not in verification and "靜態推理依據：" not in verification:
        problems.append("`## 如何驗證` 未指出對應測試或靜態推理依據")

    return problems


def main() -> int:
    code, stdout, stderr = _run_gh_pr_view()
    if code != 0:
        print("FAIL: 找不到目前分支的 GitHub PR，無法驗 PR 描述", file=sys.stderr)
        if stderr.strip():
            print(stderr.strip(), file=sys.stderr)
        return 2

    data = json.loads(stdout)
    problems = check_body(str(data.get("body") or ""))
    print(f"PR: #{data.get('number')} {data.get('url')}")
    print(f"Title: {data.get('title')}")

    if problems:
        print("FAIL: PR 描述未符合「改動有據」", file=sys.stderr)
        for problem in problems:
            print(f"- {problem}", file=sys.stderr)
        return 1

    print("PASS: PR 描述包含動機與如何驗證")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
