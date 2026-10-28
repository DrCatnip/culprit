"""Live-mode wiring uses the Token Factory client; all HTTP is mocked here."""

import asyncio
import importlib
import json
import subprocess
from dataclasses import replace

import httpx
import openai
import pytest

from culprit.cli import main
from culprit.config import DEFAULT_MODELS, TOKEN_FACTORY_URL, Settings
from culprit.demo_repo import DEMO_BUG, DEMO_EXPLANATION, DEMO_REPRO_TEST, build_demo_repo, offline_fix
from culprit.hunt import hunt


@pytest.fixture(scope="module")
def demo(tmp_path_factory):
    return build_demo_repo(tmp_path_factory.mktemp("mock-live") / "kirana-billing")


def mock_client(handler):
    return openai.OpenAI(api_key="live-mode-test-key", base_url=TOKEN_FACTORY_URL, max_retries=0,
                         http_client=httpx.Client(transport=httpx.MockTransport(handler)))


def completion(reply):
    return {"id": "test", "object": "chat.completion", "created": 0,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": json.dumps(reply)}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1000, "completion_tokens": 500, "total_tokens": 1500}}


def test_online_cli_uses_real_client_and_isolated_local_execution(demo, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("NEBIUS_API_KEY", "live-mode-test-key")
    monkeypatch.setenv("NEBIUS_PROJECT_ID", "live-mode-test-project")
    for role in ("NANO", "SUPER", "ULTRA"):
        monkeypatch.delenv("CULPRIT_MODEL_" + role, raising=False)
    monkeypatch.setenv("TOKEN_FACTORY_BASE_URL", TOKEN_FACTORY_URL)
    requests = []

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        assert request.url.host == "api.tokenfactory.nebius.com"
        system = body["messages"][0]["content"]
        user = body["messages"][1]["content"]
        if body["model"] == DEFAULT_MODELS["nano"]:
            reply = {"files": ["billing/money.py", "billing/invoice.py"]}
        elif body["model"] == DEFAULT_MODELS["ultra"]:
            reply = DEMO_EXPLANATION
        elif "test writer" in system:
            reply = {"test_code": DEMO_REPRO_TEST + '\n\ndef test_credentials_are_not_inherited():\n    import os\n    assert "NEBIUS_API_KEY" not in os.environ\n    assert "NEBIUS_PROJECT_ID" not in os.environ\n', "why": "Round half up."}
        else:
            reply = offline_fix("fix_robust" if "Style: ROBUST" in system else "fix_minimal", user)
        return httpx.Response(200, json=completion(reply))

    client = mock_client(handler)
    module = importlib.import_module("culprit.llm")
    monkeypatch.setattr(module, "make_client", lambda settings: client)
    out = tmp_path / "run"
    assert main(["hunt", "--repo", str(demo), "--bug", DEMO_BUG, "--runner", "local", "--plain", "--out", str(out)]) == 0
    data = json.loads((out / "run.json").read_text(encoding="utf-8"))
    assert data["ok"] and data["culprit"]["confirmed"]
    assert data["fixes"]["chosen"] == "minimal"
    assert {row["role"] for row in data["usage"]} == {"nano", "super", "ultra"}
    assert data["llm_cost_usd"] > 0 and all(row["calls"] > 0 for row in data["usage"])
    assert all(item["model"] != "offline-script" for item in data["models"].values())
    assert {body["model"] for body in requests} == set(DEFAULT_MODELS.values())
    assert client.is_closed
    output = capsys.readouterr().out
    assert "live-mode-test-key" not in output
    assert all("live-mode-test-key" not in path.read_text(encoding="utf-8") for path in out.iterdir())


def test_online_auth_failure_has_partial_report_without_secret(demo, tmp_path, monkeypatch):
    client = mock_client(lambda request: httpx.Response(401, json={"error": {"message": "live-mode-test-key"}}))
    module = importlib.import_module("culprit.llm")
    monkeypatch.setattr(module, "make_client", lambda settings: client)
    settings = replace(Settings.from_env(), api_key="live-mode-test-key", models=dict(DEFAULT_MODELS), base_url=TOKEN_FACTORY_URL)
    data, code = asyncio.run(hunt(demo, DEMO_BUG, settings, tmp_path / "run", runner_kind="local"))
    assert code == 1 and not data["ok"] and "authentication failed" in data["reason"]
    assert client.is_closed
    assert all("live-mode-test-key" not in path.read_text(encoding="utf-8") for path in (tmp_path / "run").iterdir())


def test_api_local_safety_rule_is_enforced(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    settings = replace(Settings.from_env(), api_key="live-mode-test-key")
    data, code = asyncio.run(hunt(tmp_path, "a bug", settings, tmp_path / "run", runner_kind="local"))
    assert code == 1 and "--i-trust-this-repo" in data["reason"]


def test_default_cli_runner_never_falls_back_to_local(demo, tmp_path, monkeypatch, capsys):
    from culprit.runners.contree_runner import RunnerError

    monkeypatch.setenv("NEBIUS_API_KEY", "live-mode-test-key")
    monkeypatch.setenv("NEBIUS_PROJECT_ID", "live-mode-test-project")
    monkeypatch.setenv("TOKEN_FACTORY_BASE_URL", TOKEN_FACTORY_URL)

    def forbidden(*args, **kwargs):
        pytest.fail("A Sandbox failure must not use local execution or call a model")

    client = mock_client(forbidden)
    monkeypatch.setattr("culprit.llm.make_client", lambda settings: client)
    monkeypatch.setattr("culprit.runners.local_runner.LocalRunner.prepare", forbidden)
    called = []

    async def sandbox_prepare(self, bundle, bad_sha):
        called.append(True)
        raise RunnerError("Sandbox access denied; no local fallback.")

    monkeypatch.setattr("culprit.runners.contree_runner.ContreeRunner.prepare", sandbox_prepare)
    out = tmp_path / "run"
    assert main(["hunt", "--repo", str(demo), "--bug", DEMO_BUG, "--out", str(out)]) == 1
    data = json.loads((out / "run.json").read_text(encoding="utf-8"))
    assert called == [True] and data["runner"]["name"] == "Nebius Sandboxes (Token Factory)"
    assert not data["ok"] and not data["usage"] and client.is_closed
    assert "live-mode-test-key" not in capsys.readouterr().out
