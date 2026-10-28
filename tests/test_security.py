"""Credentials stay on the host and out of events and reports."""

import json

from culprit.events import EventLog
from culprit.report import write_report
from culprit.security import execution_env, redact, redact_data


def test_execution_environment_strips_credentials(monkeypatch):
    monkeypatch.setenv("NEBIUS_API_KEY", "environment-test-secret")
    monkeypatch.setenv("NEBIUS_PROJECT_ID", "environment-test-project")
    monkeypatch.setenv("CONTREE_TOKEN", "sandbox-test-secret")
    monkeypatch.setenv("OTHER_API_KEY", "another-test-secret")
    monkeypatch.setenv("APP_CONFIG", "prefix-environment-test-secret-suffix")
    monkeypatch.setenv("CULPRIT_SAFE_SETTING", "keep-me")
    env = execution_env()
    assert not any(name in env for name in ("NEBIUS_API_KEY", "NEBIUS_PROJECT_ID", "CONTREE_TOKEN", "OTHER_API_KEY", "APP_CONFIG"))
    assert env["CULPRIT_SAFE_SETTING"] == "keep-me"
    assert "PATH" in env


def test_events_and_reports_redact_secrets(tmp_path, monkeypatch):
    secret = "environment-test-secret"
    monkeypatch.setenv("NEBIUS_API_KEY", secret)
    events = EventLog(tmp_path / "events.jsonl")
    events.emit("warn", msg=secret)
    assert secret not in json.dumps(events.events)
    data = {"reason": secret, "ok": False, "bug": secret, "repro": {"test_code": repr(secret)}}
    write_report(data, tmp_path)
    assert all(secret not in path.read_text(encoding="utf-8") for path in tmp_path.iterdir())
    assert "[REDACTED]" in (tmp_path / "run.json").read_text()
    assert secret in data["reason"]  # Serialization does not mutate caller data.


def test_redacts_url_userinfo_and_nested_values():
    assert "user:password" not in redact("https://user:password@example.invalid/repo.git")
    assert redact_data({"text": ["test-secret"]}, ("test-secret",)) == {"text": ["[REDACTED]"]}


def test_settings_repr_does_not_include_credentials():
    from dataclasses import replace
    from culprit.config import Settings

    settings = replace(Settings.from_env(), api_key="repr-test-secret", project_id="repr-test-project")
    assert "repr-test-secret" not in repr(settings) and "repr-test-project" not in repr(settings)
