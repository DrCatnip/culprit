"""Doctor diagnoses catalogs and safe failures without requiring live credentials."""

import asyncio
import importlib
from dataclasses import replace
from types import SimpleNamespace

import pytest

from culprit.config import DEFAULT_MODELS, Settings
from culprit.doctor import closest_model, doctor

module = importlib.import_module("culprit.doctor")


def test_missing_credentials_never_connect(monkeypatch, capsys):
    settings = replace(Settings.from_env(), api_key=None, project_id=None)

    def forbidden(*args, **kwargs):
        raise AssertionError("Missing credentials must not connect")

    monkeypatch.setattr(module, "make_client", forbidden)
    monkeypatch.setattr(module, "check_access", forbidden)
    assert asyncio.run(doctor(settings)) == 1
    output = capsys.readouterr().out
    assert "NEBIUS_API_KEY: missing" in output and "NEBIUS_PROJECT_ID: missing" in output
    assert "model checks require credentials" in output and "Sandboxes: FAILED" in output


def test_case_sensitive_suggestions():
    expected = DEFAULT_MODELS["nano"]
    assert closest_model(expected.lower(), [expected]) == expected
    assert closest_model("nvidia/Nemotron-3-Supr-120b-a12b", list(DEFAULT_MODELS.values())) == DEFAULT_MODELS["super"]
    assert closest_model("other/model", ["other/model"]) is None


@pytest.mark.parametrize("missing", [False, True])
def test_doctor_catalog_nano_and_cleanup(monkeypatch, capsys, missing):
    settings = replace(Settings.from_env(), api_key="doctor-test-key", project_id="doctor-test-project",
                       models=dict(DEFAULT_MODELS))
    called = []
    closed = []
    available = list(DEFAULT_MODELS.values())
    if missing:
        settings.models = dict(settings.models, super=DEFAULT_MODELS["super"].upper())
    client = SimpleNamespace(models=SimpleNamespace(list=lambda: SimpleNamespace(data=[SimpleNamespace(id=name) for name in available])),
                             close=lambda: closed.append(True))
    monkeypatch.setattr(module, "make_client", lambda settings: client)

    class FakeLLM:
        def __init__(self, *args, **kwargs):
            assert kwargs["client"] is client

        def chat_json(self, role, system, user, **kwargs):
            called.append((role, kwargs))
            return {"ok": True}

    async def access():
        return True, "2 images visible"

    monkeypatch.setattr(module, "TokenFactoryLLM", FakeLLM)
    monkeypatch.setattr(module, "check_access", access)
    assert asyncio.run(doctor(settings)) == (1 if missing else 0)
    output = capsys.readouterr().out
    assert "Nano JSON call: ok" in output and "Sandboxes: ok" in output
    assert called[0][0] == "nano" and called[0][1]["purpose"] == "doctor"
    assert closed == [True]
    assert "doctor-test-key" not in output
    if missing:
        assert "did you mean " + DEFAULT_MODELS["super"] in output


def test_doctor_network_failure_is_sanitized(monkeypatch, capsys):
    settings = replace(Settings.from_env(), api_key="doctor-secret-value", project_id=None)
    closed = []

    def failed():
        raise RuntimeError("headers contained doctor-secret-value")

    client = SimpleNamespace(models=SimpleNamespace(list=failed), close=lambda: closed.append(True))
    monkeypatch.setattr(module, "make_client", lambda settings: client)
    assert asyncio.run(doctor(settings)) == 1
    output = capsys.readouterr().out
    assert "service operation failed" in output and "doctor-secret-value" not in output
    assert closed == [True]
