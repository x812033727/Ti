"""文件契約守護：`health_check` 的 docstring 必含「沿用 run_http_demo」與「is-active」
兩個 grep 錨點——這是設計決策留下的文件契約，CI 跑 pytest 順帶守護不被無聲改掉。

背景：health_check 早夭判定採「沿用 runner.run_http_demo 的早夭偵測」語意（見
`studio/deploy.py::health_check` docstring），其落實方式為 `systemctl is-active` 探針。
兩個字串是這條決策的最小文件足跡，缺一就視為契約被破壞——CI 跑 pytest 必紅，提示接手者
補回來源說明或重新評估決策。
"""

from __future__ import annotations

from pathlib import Path


def test_health_check_docstring_contains_required_strings():
    src = (
        Path(__file__).resolve().parent.parent.parent / "studio" / "deploy.py"
    )
    text = src.read_text(encoding="utf-8")
    assert "沿用 run_http_demo" in text, (
        "deploy.py 必須在 health_check 的 docstring 點名『沿用 run_http_demo 的早夭偵測』——"
        "這是設計決策的最小文件足跡，被移除代表契約被破壞"
    )
    assert "is-active" in text, (
        "deploy.py 必須在 health_check 提到『is-active』——這是早夭判定的真值來源名稱，"
        "被移除代表接手者失去判斷『為何要這樣寫』的最短線索"
    )
