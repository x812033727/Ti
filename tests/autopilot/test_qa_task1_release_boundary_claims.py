"""QA 任務 #1：release pipeline 文件收斂後的邊界宣稱測試。

這批測試故意不打 GitHub API；它只驗證已落盤的 workflow、audit、handoff 與
evidence JSON 是否能支撐目前宣稱。目的不是再跑一次快樂路徑，而是防止兩種常見假綠：

1. 把 v0.2.0 的一次性生產證據外推成「後續版本已閉環」。
2. 把只能攔空 secret 的 PAT guard 誤寫成「過期/撤銷 PAT 也能提前 fail-fast」。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
AUDIT = ROOT / "docs" / "release-pipeline-requirements-audit.md"
HANDOFF = ROOT / "docs" / "release-e2e-handoff.md"
PUBLISH_YML = ROOT / ".github" / "workflows" / "publish-release.yml"
SMOKE_YML = ROOT / ".github" / "workflows" / "release-smoke.yml"
ONLINE_BODY = ROOT / "docs" / "evidence" / "release-v0.2.0-online-body.json"
BODY_VERDICT = ROOT / "docs" / "evidence" / "release-v0.2.0-body-structure-verdict.json"
SMOKE_TRIGGER = ROOT / "docs" / "evidence" / "release-smoke-v0.2.0-trigger.json"


def _table_rows(text: str) -> list[str]:
    return [
        line
        for line in text.splitlines()
        if line.startswith("|")
        and line.count("|") >= 4
        and not re.match(r"^\|\s*:?-+:?\s*\|", line)
    ]


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_v020_production_evidence_is_double_sourced_and_release_triggered():
    """v0.2.0 已閉環宣稱必須有 body 雙來源與 release webhook 成功 run 兩種證據。"""
    online_body = _read_json(ONLINE_BODY)
    body_verdict = _read_json(BODY_VERDICT)
    smoke_trigger = _read_json(SMOKE_TRIGGER)

    assert online_body["tag"] == "v0.2.0"
    assert online_body["body_match"] is True
    assert online_body["tag_match"] is True
    assert re.fullmatch(r"[0-9a-f]{64}", online_body["body_sha256"])

    assert body_verdict["tag"] == "v0.2.0"
    assert body_verdict["verdict"] == "PASS"
    assert body_verdict["checks"]["頂部即 Breaking 置頂"] is True
    assert body_verdict["checks"]["雙來源正規化後逐字相等(gh vs REST)"] is True
    assert not body_verdict["problems"]

    assert smoke_trigger["tag"] == "v0.2.0"
    assert smoke_trigger["event"] == "release"
    assert smoke_trigger["status"] == "completed"
    assert smoke_trigger["conclusion"] == "success"
    assert smoke_trigger["workflow_path"] == ".github/workflows/release-smoke.yml"
    cross_checks = smoke_trigger["cross_checks"]
    assert cross_checks["gh_rest_event_match"] is True
    assert cross_checks["workflow_path_is_release_smoke_yml"] is True
    assert cross_checks["event_is_release_webhook"] is True
    assert cross_checks["success_run_verifies_release_published_trigger"] is True


def test_audit_does_not_overclaim_future_versions_as_closed():
    """audit 可說 v0.2.0 已閉環，但後續版本仍必須是逐版 E2E 缺口。"""
    rows = _table_rows(AUDIT.read_text(encoding="utf-8"))

    v020_rows = [row for row in rows if "v0.2.0" in row and "生產證據" in row]
    assert v020_rows, "缺 v0.2.0 生產證據列"
    assert any("已達成" in row for row in v020_rows), v020_rows
    assert any("release-smoke-v0.2.0-trigger.json" in row for row in v020_rows), v020_rows

    future_rows = [row for row in rows if "後續版本" in row and "端到端" in row]
    assert future_rows, "缺後續版本真實端到端缺口列"
    assert all("缺口" in row for row in future_rows), future_rows
    assert not any("已達成" in row for row in future_rows), future_rows
    assert any("逐版" in row and ("不得" in row or "外推" in row) for row in future_rows), (
        future_rows
    )


def test_pat_expiry_is_documented_as_late_403_not_preflight_success():
    """空 GH_PAT 可 fail-fast；過期/撤銷 PAT 不得被文件標成已提前攔截。"""
    audit_rows = _table_rows(AUDIT.read_text(encoding="utf-8"))
    publish = PUBLISH_YML.read_text(encoding="utf-8")

    empty_secret_rows = [row for row in audit_rows if "GH_PAT" in row and "未設" in row]
    assert empty_secret_rows, "缺 GH_PAT 未設 fail-fast 列"
    assert any("已達成" in row for row in empty_secret_rows), empty_secret_rows

    expiry_rows = [row for row in audit_rows if "PAT 過期/撤銷" in row]
    assert expiry_rows, "缺 PAT 過期/撤銷邊界列"
    assert all("缺口" in row for row in expiry_rows), expiry_rows
    assert not any("已達成" in row for row in expiry_rows), expiry_rows
    assert any("403" in row and "Create release" in row for row in expiry_rows), expiry_rows

    verify_pat_block = re.search(
        r"# Step 1 .*?Verify PAT(?P<body>.*?)(?=\n\s*# Step 2|\Z)",
        publish,
        flags=re.S,
    )
    assert verify_pat_block, "publish workflow 缺 Verify PAT step"
    block = verify_pat_block.group(0)
    assert 'test -n "$GH_TOKEN"' in block
    assert "403" in block
    assert "過期" in block or "撤銷" in block


def test_workflow_contract_still_uses_pat_to_create_and_release_event_to_smoke():
    """核心鏈不能退回 GITHUB_TOKEN 建 release，也不能把 smoke 改回 tag push。"""
    publish = PUBLISH_YML.read_text(encoding="utf-8")
    smoke = SMOKE_YML.read_text(encoding="utf-8")

    create_release = re.search(
        r"- name: Create release(?P<body>.*?)(?=\n\s*-\s+name:|\Z)",
        publish,
        flags=re.S,
    )
    assert create_release, "publish workflow 缺 Create release step"
    body = create_release.group("body")
    assert "GH_TOKEN: ${{ secrets.GH_PAT }}" in body
    assert 'gh release create "$TAG" -F body.md' in body
    assert "github.token" not in body
    assert "GITHUB_TOKEN" not in body

    assert re.search(r"(?m)^\s*release:\s*$", smoke)
    assert re.search(r"(?m)^\s*types:\s*\[published\]\s*$", smoke)
    assert not re.search(r"(?m)^\s*push:\s*$", smoke), "release-smoke 不應改回 push tag 觸發"


def test_handoff_keeps_manual_checklist_for_post_v020_versions():
    """handoff 必須同時說清 v0.2.0 已封口，以及新版本仍要人工核對。"""
    handoff = HANDOFF.read_text(encoding="utf-8")

    assert "v0.2.0 此鏈已生產閉環" in handoff
    assert "後續版本仍半閉環" in handoff
    assert "docs/evidence/release-v0.2.0-online-body.json" in handoff
    assert "docs/evidence/release-smoke-v0.2.0-trigger.json" in handoff
    assert "release-smoke.yml" in handoff
    assert "secrets.GH_PAT" in handoff
    assert "第一次正式打 `v*` tag 後" in handoff
    assert "任一項不符即視為 E2E 未通過" in handoff
