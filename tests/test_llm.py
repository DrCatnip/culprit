"""Exercise the real OpenAI-compatible client with an HTTP mock, never live APIs."""

import json
from dataclasses import replace

import httpx
import openai
import pytest

from culprit.config import DEFAULT_MODELS, TOKEN_FACTORY_URL, Settings, load_dotenv
from culprit.events import EventLog
from culprit.llm import LLMError, TokenFactoryLLM


def response(content='{"ok": true}', **extra):
    return {"id": "test", "object": "chat.completion", "created": 0,
            "model": DEFAULT_MODELS["nano"],
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1000, "completion_tokens": 500, "total_tokens": 1500}, **extra}


def model(handler, settings=None):
    settings = settings or replace(Settings.from_env(), api_key="test-key-not-a-real-secret",
                                   base_url=TOKEN_FACTORY_URL, models=dict(DEFAULT_MODELS))
    client = openai.OpenAI(api_key=settings.api_key, base_url=settings.base_url, max_retries=0,
                           http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    events = EventLog()
    return TokenFactoryLLM(settings, events, client=client), events


def test_nebius_endpoint_json_usage_and_nano_budget():
    requests = []

    def handler(request):
        requests.append(request)
        assert request.url.host == "api.tokenfactory.nebius.com"
        assert request.url.path == "/v1/chat/completions"
        body = json.loads(request.content)
        assert body["model"] == DEFAULT_MODELS["nano"]
        assert body["response_format"] == {"type": "json_object"}
        assert body["max_tokens"] == 4000 and body["temperature"] == 0.2
        return httpx.Response(200, json=response('<think>reasoning</think>\n```json\n{"ok": true}\n```'))

    llm, events = model(handler)
    try:
        assert llm.chat_json("nano", "system", "user", "test") == {"ok": True}
        usage = llm.usage.rows()[0]
        assert usage["calls"] == 1 and usage["prompt_tokens"] == 1000
        assert usage["completion_tokens"] == 500 and usage["cost_usd"] > 0
        assert events.events[-1]["kind"] == "model_finished" and events.events[-1]["ok"]
        assert len(requests) == 1
    finally:
        llm.close()


def test_json_mode_fallback_and_parse_retry():
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        if len(bodies) == 1:
            return httpx.Response(400, json={"error": {"message": "response_format json_object is unsupported"}})
        return httpx.Response(200, json=response("not JSON" if len(bodies) == 2 else '{"fixed": true}'))

    llm, _ = model(handler)
    try:
        assert llm.chat_json("super", "system", "user", "test") == {"fixed": True}
        assert "response_format" in bodies[0]
        assert "response_format" not in bodies[1] and "response_format" not in bodies[2]
        assert "ONLY the JSON object" in bodies[2]["messages"][-1]["content"]
        assert llm.usage.rows()[0]["calls"] == 2
    finally:
        llm.close()


@pytest.mark.parametrize("empty_choices", [False, True])
def test_invalid_reply_is_bounded_and_controlled(empty_choices):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=response("invalid", **({"choices": []} if empty_choices else {})))

    llm, events = model(handler)
    try:
        with pytest.raises(LLMError, match="invalid JSON reply"):
            llm.chat_json("super", "system", "user", "test")
        assert len(calls) == 4 and llm.usage.rows()[0]["calls"] == 4
        assert events.events[-1]["ok"] is False
    finally:
        llm.close()


@pytest.mark.parametrize("status", [400, 401, 403, 404, 429, 503])
def test_http_errors_never_echo_response_secrets(status):
    secret = "test-key-not-a-real-secret"
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, json={"error": {"message": "server echoed " + secret}})

    llm, events = model(handler)
    try:
        with pytest.raises(LLMError) as caught:
            llm.chat_json("super", "system", "user", "test")
        assert secret not in str(caught.value)
        assert str(status) in str(caught.value)
        assert secret not in json.dumps(events.events)
        assert len(calls) == 1
    finally:
        llm.close()


def test_prompts_and_reply_are_redacted():
    secret = "test-key-not-a-real-secret"

    def handler(request):
        body = json.loads(request.content)
        assert secret not in json.dumps(body["messages"])
        return httpx.Response(200, json=response(json.dumps({"text": secret})))

    llm, _ = model(handler)
    try:
        assert llm.chat_json("super", secret, secret, "test") == {"text": "[REDACTED]"}
    finally:
        llm.close()


def test_other_provider_endpoint_is_rejected_before_client_creation(monkeypatch):
    def forbidden(**kwargs):
        raise AssertionError("Invalid endpoint must not construct a client")

    monkeypatch.setattr(openai, "OpenAI", forbidden)
    settings = replace(Settings.from_env(), api_key="test-only", base_url="https://api.openai.com/v1/")
    with pytest.raises(LLMError, match="api.tokenfactory.nebius.com"):
        TokenFactoryLLM(settings, EventLog())


def test_other_models_are_rejected_without_request():
    def forbidden(request):
        raise AssertionError("Unsupported model must not make a request")

    settings = replace(Settings.from_env(), api_key="test-only", models={"nano": "other/model"})
    llm, _ = model(forbidden, settings)
    try:
        with pytest.raises(LLMError, match="NVIDIA Nemotron"):
            llm.chat_json("nano", "system", "user", "test")
    finally:
        llm.close()


def test_dotenv_does_not_override_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("NEBIUS_API_KEY", "environment-test-key")
    monkeypatch.setenv("CULPRIT_MODEL_SUPER", "restore-after-test")
    monkeypatch.delenv("CULPRIT_MODEL_SUPER")
    path = tmp_path / ".env"
    path.write_text('NEBIUS_API_KEY=file-test-key\nCULPRIT_MODEL_SUPER="nvidia/Nemotron-Test"\n')
    load_dotenv(path)
    settings = Settings.from_env()
    assert settings.api_key == "environment-test-key"
    assert settings.models["super"] == "nvidia/Nemotron-Test"
