"""任務 #1 QA：Email notify sink 驗收合約。

重點不是再測 SMTP 細節，而是驗證產品層行為：
- email-only 也能完成紅色告警演練，且 delivery 證據落盤；
- email-only 在自治狀態 API 被視為外部通知已配置；
- 寄出的 email 內容不得洩漏已知秘密。
"""

from __future__ import annotations

from email import message_from_string

from fastapi.testclient import TestClient

from studio import config, notify


class _FakeSMTP:
    calls: list[dict] = []

    def __init__(self, host, port, timeout=None):
        self.call = {
            "host": host,
            "port": port,
            "timeout": timeout,
            "starttls": False,
            "sendmail": None,
        }
        self.calls.append(self.call)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def starttls(self):
        self.call["starttls"] = True

    def sendmail(self, sender, recipients, message):
        self.call["sendmail"] = (sender, recipients, message)


def _reset_sinks(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "AUTOPILOT_STATE_DIR", tmp_path / "ap")
    monkeypatch.setattr(config, "NOTIFY_WEBHOOK", "")
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "")
    monkeypatch.setattr(config, "ALERT_EMAIL_TO", "")
    monkeypatch.setattr(config, "ALERT_SMTP_HOST", "")
    monkeypatch.setattr(config, "ALERT_SMTP_PORT", 587)
    monkeypatch.setattr(config, "ALERT_SMTP_USER", "")
    monkeypatch.setattr(config, "ALERT_SMTP_PASS", "")
    monkeypatch.setattr(config, "ALERT_FROM", "Ti Studio <noreply@localhost>")
    monkeypatch.setattr(config, "NOTIFY_TIMEOUT", 3.5)
    _FakeSMTP.calls = []
    monkeypatch.setattr(notify.smtplib, "SMTP", _FakeSMTP)
    monkeypatch.setattr(notify.smtplib, "SMTP_SSL", _FakeSMTP)


def _enable_email_only(monkeypatch):
    monkeypatch.setattr(config, "ALERT_EMAIL_TO", "ops@example.test, qa@example.test")
    monkeypatch.setattr(config, "ALERT_SMTP_HOST", "smtp.example.test")


def _raw_email(call: dict) -> str:
    assert call["sendmail"] is not None
    return call["sendmail"][2]


def _email_body(raw: str) -> str:
    msg = message_from_string(raw)
    payload = msg.get_payload(decode=True)
    assert isinstance(payload, bytes)
    return payload.decode(msg.get_content_charset() or "utf-8")


def test_red_drills_email_only_delivers_all_kinds_and_persists_evidence(tmp_path, monkeypatch):
    _reset_sinks(monkeypatch, tmp_path)
    _enable_email_only(monkeypatch)

    result = notify.send_red_drills()

    assert result["ok"] is True
    assert set(result["results"]) == set(notify.RED_DRILL_KINDS)
    assert all(sinks == {"email": True} for sinks in result["results"].values())
    assert len(_FakeSMTP.calls) == len(notify.RED_DRILL_KINDS)
    assert all(call["starttls"] is True for call in _FakeSMTP.calls)
    assert all(call["timeout"] == 3.5 for call in _FakeSMTP.calls)

    deliveries = notify.read_deliveries(1)
    assert len(deliveries) == len(notify.RED_DRILL_KINDS)
    assert {row["sink"] for row in deliveries} == {"email"}
    assert {row["alert_kind"] for row in deliveries} == set(notify.RED_DRILL_KINDS)
    assert all(row["ok"] is True and row["drill"] is True for row in deliveries)


def test_autonomy_status_api_counts_email_only_as_external_sink(tmp_path, monkeypatch):
    _reset_sinks(monkeypatch, tmp_path)
    _enable_email_only(monkeypatch)
    monkeypatch.setattr(config, "PROJECTS_ROOT", tmp_path / "projects")
    monkeypatch.setattr(config, "WORKSPACE_ROOT", tmp_path / "ws")
    monkeypatch.setattr(config, "ACCESS_PASSWORD", "")
    from studio.server import app

    client = TestClient(app, client=("127.0.0.1", 42001))
    response = client.get("/api/autonomy/status")

    assert response.status_code == 200
    notification = response.json()["platform"]["notification"]
    assert notification["webhook_configured"] is False
    assert notification["telegram_configured"] is False
    assert notification["email_configured"] is True
    assert notification["external_sink_configured"] is True


def test_email_sink_redacts_secret_material_before_smtp(tmp_path, monkeypatch):
    _reset_sinks(monkeypatch, tmp_path)
    _enable_email_only(monkeypatch)
    monkeypatch.setattr(config, "ALERT_SMTP_PASS", "smtp-secret")
    monkeypatch.setattr(config, "GITHUB_TOKEN", "ghp_secret_token")

    ok = notify.send(
        "quota_exhausted",
        "quota smtp-secret ghp_secret_token",
        token="ghp_secret_token",
        plain_path="/tmp/private/file.txt",
    )

    assert ok is True
    raw = _raw_email(_FakeSMTP.calls[0])
    body = _email_body(raw)
    assert "smtp-secret" not in raw
    assert "ghp_secret_token" not in raw
    assert "/tmp/private/file.txt" not in raw
    assert "***" in raw
    assert "token: {'configured': True}" in body
    assert "plain_path: [redacted-path]" in body
