"""Exercise real git history and isolated tests, with network access forbidden."""

import asyncio
import json
import socket
import subprocess
from pathlib import Path

import pytest

from culprit.cli import main
from culprit.config import Settings
from culprit.demo_repo import CULPRIT_SUBJECT, DEMO_BUG, build_demo_repo
from culprit.gitops import git
from culprit.hunt import hunt
from culprit.runners.base import failure_types
from culprit.llm import LLMError, OfflineDemoLLM
from culprit.events import EventLog


@pytest.fixture(scope="module")
def demo(tmp_path_factory):
    return build_demo_repo(tmp_path_factory.mktemp("offline") / "kirana-billing")


@pytest.fixture
def no_network(monkeypatch):
    for key in ("NEBIUS_API_KEY", "NEBIUS_PROJECT_ID", "NEBIUS_AI_PROJECT"):
        monkeypatch.delenv(key, raising=False)

    def forbidden(*args, **kwargs):
        raise AssertionError("Offline mode attempted network access")

    original_socketpair = socket.socketpair
    original_connect = socket.socket.connect

    def socketpair(*args, **kwargs):
        # Windows implements asyncio's internal wakeup pipe with a loopback pair.
        # Only that stdlib operation may connect; application connections stay blocked.
        with monkeypatch.context() as patch:
            patch.setattr(socket.socket, "connect", original_connect)
            return original_socketpair(*args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "socketpair", socketpair)
    monkeypatch.setattr("culprit.llm.TokenFactoryLLM", forbidden)
    monkeypatch.setattr("culprit.runners.contree_runner.ContreeRunner", forbidden)
    monkeypatch.setenv("PIP_NO_INDEX", "1")
    monkeypatch.setenv("PIP_DISABLE_PIP_VERSION_CHECK", "1")


def test_full_offline_hunt(demo, tmp_path, no_network):
    out = tmp_path / "run"
    data, code = asyncio.run(hunt(demo, DEMO_BUG, Settings.from_env(), out, offline=True))
    assert code == 0 and data["ok"], data["reason"]
    assert data["culprit"]["commit"]["subject"] == CULPRIT_SUBJECT
    assert data["culprit"]["confirmed"] and not data["culprit"]["ambiguous"]
    assert failure_types(data["culprit"]["output"]) == {"AssertionError"}
    assert data["repo"]["good_found_by"] == "scout"
    assert 1 <= len(data["rounds"]) <= 3
    assert any(entry["status"] == "pass" for entry in data["scout"])
    assert data["repro"]["attempts"][0]["bad_status"] == "fail"
    assert "round(paise / 100)" in data["explanation"]["root_cause"]
    assert all("offline stand-in" in model["label"] for model in data["models"].values())
    assert data["llm_cost_usd"] == 0
    candidates = {candidate["name"]: candidate for candidate in data["fixes"]["candidates"]}
    assert candidates["minimal"]["passed"] and candidates["minimal"]["chosen"]
    assert candidates["minimal"]["lines"] == 3
    assert candidates["robust"]["passed"]
    assert not candidates["revert"]["passed"] and candidates["revert"]["suite_code"] != 0
    assert "test_export.py" in candidates["revert"]["output"]
    assert "to_paise" in candidates["revert"]["output"]
    assert data["fixes"]["chosen"] == "minimal"
    saved = json.loads((out / "run.json").read_text(encoding="utf-8"))
    fields = "version started_at finished_at seconds ok reason repo bug runner models usage llm_cost_usd context repro scout range rounds culprit explanation fixes comparison warnings".split()
    assert set(saved) == set(fields)
    assert {p.name for p in out.iterdir()} == {
        "run.json", "events.jsonl", "report.md", "report.html", "fix.patch", "test_culprit_repro.py",
    }
    assert "offline stand-in" in (out / "report.md").read_text(encoding="utf-8").lower()
    assert CULPRIT_SUBJECT in (out / "report.html").read_text(encoding="utf-8")
    fresh = build_demo_repo(tmp_path / "fresh")
    git(fresh, "apply", "--check", str(out / "fix.patch"))
    git(fresh, "apply", str(out / "fix.patch"))
    import os
    import sys
    result = subprocess.run([sys.executable, "-m", "pytest", "-q"], cwd=fresh, capture_output=True, text=True,
                            env=dict(os.environ, PYTHONPATH=str(fresh)))
    assert result.returncode == 0, result.stdout + result.stderr
    events = [json.loads(line) for line in (out / "events.jsonl").read_text().splitlines()]
    assert events[-1]["kind"] == "run_finished" and events[-1]["ok"]
    assert {entry["name"] for entry in events if entry["kind"] == "stage"} == {
        "prepare", "context", "repro", "scout", "bisect", "confirm", "explain", "fix", "report",
    }


def test_explicit_good_and_no_fix(demo, tmp_path, no_network):
    data, code = asyncio.run(hunt(demo, DEMO_BUG, Settings.from_env(), tmp_path / "run",
                                 good="v1.2.0", offline=True, no_fix=True))
    assert code == 0 and data["culprit"]["confirmed"]
    assert data["repo"]["good_found_by"] == "user" and not data["scout"]
    assert data["repro"]["attempts"][0]["good_status"] == "pass"
    assert not data["fixes"]["candidates"] and not (tmp_path / "run/fix.patch").exists()


def test_wrong_good_writes_partial_report(demo, tmp_path, no_network):
    data, code = asyncio.run(hunt(demo, DEMO_BUG, Settings.from_env(), tmp_path / "run",
                                 good="HEAD~1", offline=True))
    assert code == 2 and not data["ok"]
    assert "already present" in data["reason"]
    assert (tmp_path / "run/report.html").exists()


def test_cli_safety_before_execution(tmp_path, no_network, capsys):
    assert main(["hunt", "--repo", str(tmp_path), "--bug", "wrong", "--runner", "local"]) == 1
    assert "--i-trust-this-repo" in capsys.readouterr().out
    assert main(["hunt", "--repo", str(tmp_path), "--bug", "wrong", "--offline"]) == 1
    assert "only works with the bundled demo" in capsys.readouterr().out


def test_reproduction_and_minimal_fix_retry(demo, tmp_path, no_network):
    from culprit.demo_repo import MONEY_AFTER

    class RetryDemo(OfflineDemoLLM):
        repro_calls = 0
        minimal_calls = 0

        def chat_json(self, role, system, user, purpose, **kwargs):
            if purpose == "explain_culprit" and role == "ultra":
                raise LLMError("Explanation unavailable")
            if purpose == "write_repro_test":
                self.repro_calls += 1
                if self.repro_calls == 1:
                    return {"test_code": "def test_passes():\n    assert True\n", "why": "A test that needs correction."}
                assert "PASSED on the buggy commit" in user
            if purpose == "fix_minimal":
                self.minimal_calls += 1
            if purpose == "fix_robust" or purpose == "fix_minimal" and self.minimal_calls == 1:
                return {"files": [{"path": "billing/money.py", "content": MONEY_AFTER}], "summary": "Unchanged buggy code."}
            if purpose == "fix_minimal":
                assert "Your fix did not pass" in user
            return super().chat_json(role, system, user, purpose, **kwargs)

    events = EventLog(tmp_path / "run/events.jsonl")
    llm = RetryDemo(demo, events)
    data, code = asyncio.run(hunt(demo, DEMO_BUG, Settings.from_env(), tmp_path / "run",
                                 good="v1.2.0", offline=True, llm=llm, events=events))
    assert code == 0 and len(data["repro"]["attempts"]) == 2
    assert data["repro"]["attempts"][0]["bad_status"] == "pass"
    assert data["fixes"]["chosen"] == "minimal_retry"
    assert not data["fixes"]["candidates"][0]["passed"]
    assert not data["fixes"]["candidates"][1]["passed"]
    assert data["fixes"]["candidates"][-1]["passed"]
    assert "Super" in data["explanation"]["model"]
    assert any("using Super" in warning for warning in data["warnings"])


def test_cancellation_writes_partial_report_and_closes_runner(demo, tmp_path, no_network):
    from culprit.runners.local_runner import LocalRunner

    class WaitingRunner(LocalRunner):
        closed = False

        async def probe(self, *args, **kwargs):
            started.set()
            await asyncio.Future()

        async def close(self):
            self.closed = True
            await super().close()

    async def run():
        nonlocal started
        started = asyncio.Event()
        runner = WaitingRunner()
        task = asyncio.create_task(hunt(demo, DEMO_BUG, Settings.from_env(), tmp_path / "run",
                                        offline=True, runner=runner))
        await asyncio.wait_for(started.wait(), 20)
        task.cancel()
        data, code = await task
        assert runner.closed and not runner.tmp.exists()
        return data, code

    started = None
    data, code = asyncio.run(run())
    assert code == 1 and not data["ok"] and "Interrupted" in data["reason"]
    assert (tmp_path / "run/report.html").exists()
    events = [json.loads(line) for line in (tmp_path / "run/events.jsonl").read_text().splitlines()]
    assert any(event["kind"] == "stage" and event["name"] == "repro" and event["status"] == "failed" for event in events)
    assert events[-1]["kind"] == "run_finished" and not events[-1]["ok"]


def test_reproduction_exhaustion_emits_failed_stage(demo, tmp_path, no_network):
    class PassingTestDemo(OfflineDemoLLM):
        def chat_json(self, role, system, user, purpose, **kwargs):
            if purpose == "write_repro_test":
                return {"test_code": "def test_passes():\n    assert True\n", "why": "Cannot reproduce."}
            return super().chat_json(role, system, user, purpose, **kwargs)

    events = EventLog(tmp_path / "run/events.jsonl")
    settings = Settings.from_env()
    settings.max_test_attempts = 2
    data, code = asyncio.run(hunt(demo, DEMO_BUG, settings, tmp_path / "run", offline=True,
                                 llm=PassingTestDemo(demo, events), events=events))
    assert code == 2 and not data["ok"] and len(data["repro"]["attempts"]) == 2
    assert all(attempt["bad_status"] == "pass" for attempt in data["repro"]["attempts"])
    assert any(event["kind"] == "stage" and event["name"] == "repro" and event["status"] == "failed" for event in events.events)
    assert events.events[-1]["kind"] == "run_finished" and not events.events[-1]["ok"]
