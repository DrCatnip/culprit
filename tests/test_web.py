"""The optional web adapter runs the existing engine and rejects arbitrary input."""

import asyncio
import json
import time
from pathlib import Path

import pytest

pytest.importorskip("flask")

from culprit.demo_repo import CULPRIT_SUBJECT, DEMO_BUG, build_demo_repo
from culprit.gitops import git
from culprit.web import create_app


@pytest.fixture
def app(tmp_path):
    application = create_app({"TESTING": True, "RUNS_ROOT": tmp_path / "runs", "DEMO_REPO": tmp_path / "demo"})
    yield application
    application.extensions["culprit_jobs"].close()


def wait_for(client, url, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(url)
        assert response.status_code == 200
        data = response.get_json()
        if data["status"] != "running":
            return data
        time.sleep(0.03)
    pytest.fail("Web investigation did not finish in time")


def test_home_health_and_configuration(app):
    client = app.test_client()
    home = client.get("/")
    assert home.status_code == 200
    for text in (b"Autonomous Git Bug Investigator", b"Run Demo Investigation", b"OFFLINE DEMO MODE", b"G2 remains BLOCKED"):
        assert text in home.data
    assert "script-src 'self'" in home.headers["Content-Security-Policy"]
    assert client.get("/health").get_json() == {"status": "ok", "mode": "offline-demo"}
    config = client.get("/api/demo").get_json()
    assert config["bug"] == DEMO_BUG and config["repository"] == "kirana-billing"
    assert config["parallel"] == 2 and config["mode"] == "offline"
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/app.css").status_code == 200


@pytest.mark.parametrize("payload", [[], None, "text", {"repo": "/etc"}, {"repository": "https://example.invalid/repo.git"},
                                     {"repository": "../../"}, {"mode": "live"}, {"bug": "run arbitrary code"},
                                     {"command": "echo nope"}, {"out": "/tmp/escape"}])
def test_malformed_or_arbitrary_input_never_starts_work(app, monkeypatch, payload):
    def forbidden():
        pytest.fail("Invalid browser input reached the engine")

    monkeypatch.setattr(app.extensions["culprit_jobs"], "start", forbidden)
    assert app.test_client().post("/api/demo", data=json.dumps(payload), content_type="application/json").status_code == 400


def test_request_limits_and_cross_origin(app):
    client = app.test_client()
    assert client.post("/api/demo", data="command=nope").status_code == 415
    assert client.post("/api/demo", data="{", content_type="application/json").status_code == 400
    assert client.post("/api/demo", json={}, headers={"Origin": "https://other.invalid"}).status_code == 403
    assert client.post("/api/demo", data='"' + 'a' * 5000 + '"', content_type="application/json").status_code == 413
    assert client.get("/api/demo/../../.env").status_code == 404
    assert client.get("/api/demo/" + "a" * 32).status_code == 404


def test_api_job_events_result_and_artifact_guards(app, monkeypatch):
    import culprit.web as web

    monkeypatch.setattr(web, "is_demo_repo", lambda path: True)
    monkeypatch.setenv("NEBIUS_API_KEY", "synthetic-web-secret")
    monkeypatch.setenv("NEBIUS_PROJECT_ID", "synthetic-web-project")

    async def engine(repo, bug, settings, directory, **kwargs):
        assert kwargs["offline"] is True and settings.api_key is None and settings.project_id is None
        assert bug == DEMO_BUG and repo == Path(app.config["DEMO_REPO"]).resolve()
        kwargs["events"].emit("stage", name="repro", status="running", detail="synthetic-web-secret")
        kwargs["events"].emit("stage", name="repro", status="done", detail="synthetic-web-project")
        return {"ok": True, "repo": {"name": "demo", "source": str(repo)}, "bug": DEMO_BUG,
                "models": {"super": {"model": "offline-script", "label": "Offline stand-in"}},
                "culprit": {"commit": {"short": "example", "subject": "synthetic-web-secret"}, "confirmed": True},
                "fixes": {"candidates": [{"name": "minimal", "chosen": True, "passed": True, "patch": "synthetic-web-project",
                                          "output": "=== CULPRIT REPRO ===\n1 passed in 0.1s\n=== CULPRIT SUITE ===\n12 passed in 0.2s\n=== CULPRIT CHANGES ===\n"},
                                         {"name": "revert", "passed": False, "output": "1 passed in 0.1s\n=== CULPRIT SUITE ===\nImportError"}]}}, 0

    monkeypatch.setattr(web, "hunt", engine)
    client = app.test_client()
    started = client.post("/api/demo", json={})
    assert started.status_code == 202
    job = wait_for(client, started.get_json()["status_url"])
    assert job["status"] == "complete" and job["result"]["ok"]
    assert job["test_counts"] == {"minimal": 12, "revert": None}
    assert job["result"]["repo"]["source"] == "Bundled kirana-billing demo"
    assert "synthetic-web-secret" not in json.dumps(job) and "synthetic-web-project" not in json.dumps(job)
    report = client.get(job["report_url"])
    assert report.status_code == 200 and b"Offline stand-in" in report.data
    assert "sandbox" in report.headers["Content-Security-Policy"]
    for url in job["artifacts"].values():
        response = client.get(url)
        assert response.status_code == 200 and "attachment" in response.headers["Content-Disposition"]
        assert b"synthetic-web-secret" not in response.data and b"synthetic-web-project" not in response.data
    identity = job["id"]
    assert client.get(f"/api/demo/{identity}/artifacts/.env").status_code == 404
    assert client.get(f"/api/demo/{identity}?after=nope").status_code == 400
    assert client.get(f"/api/demo/{identity}?after=-1").status_code == 400
    assert client.get(f"/api/demo/{identity}?after={job['cursor']}").get_json()["events"] == []


def test_running_jobs_are_joined_instead_of_queued(app, monkeypatch):
    import culprit.web as web
    import threading

    release = threading.Event()
    monkeypatch.setattr(web, "is_demo_repo", lambda path: True)

    async def engine(*args, **kwargs):
        await asyncio.to_thread(release.wait, 10)
        return {"ok": False, "reason": "Stopped for this test", "repo": {}}, 2

    monkeypatch.setattr(web, "hunt", engine)
    client = app.test_client()
    try:
        one = client.post("/api/demo", json={}).get_json()
        two = client.post("/api/demo", json={}).get_json()
        assert one["id"] == two["id"] and not one["reused"] and two["reused"]
    finally:
        release.set()
    result = wait_for(client, one["status_url"])
    assert result["status"] == "failed" and result["report_url"]


def test_worker_and_request_errors_do_not_leak_secrets(app, monkeypatch):
    import culprit.web as web

    monkeypatch.setattr(web, "is_demo_repo", lambda path: True)

    async def fail(*args, **kwargs):
        raise RuntimeError("Authorization: Bearer synthetic-secret-in-exception")

    monkeypatch.setattr(web, "hunt", fail)
    client = app.test_client()
    started = client.post("/api/demo", json={}).get_json()
    job = wait_for(client, started["status_url"])
    assert job["status"] == "failed" and "synthetic-secret" not in json.dumps(job)
    assert job["report_url"] is None


def test_real_web_demo_uses_existing_engine(tmp_path, monkeypatch):
    # This is real local execution. HTTP/model/Sandbox success is not inferred.
    repo = build_demo_repo(tmp_path / "demo" / "kirana-billing")
    before = git(repo, "status", "--porcelain")
    head = git(repo, "rev-parse", "HEAD")
    monkeypatch.setenv("PIP_NO_INDEX", "1")
    app = create_app({"TESTING": True, "RUNS_ROOT": tmp_path / "runs", "DEMO_REPO": repo})
    jobs = app.extensions["culprit_jobs"]
    try:
        client = app.test_client()
        started = client.post("/api/demo", json={})
        assert started.status_code == 202
        result = wait_for(client, started.get_json()["status_url"], 180)
        assert result["status"] == "complete", result["error"]
        data = result["result"]
        assert data["culprit"]["confirmed"] and data["culprit"]["commit"]["subject"] == CULPRIT_SUBJECT
        assert data["fixes"]["chosen"] == "minimal"
        assert result["test_counts"]["minimal"] == 12 and result["test_counts"]["revert"] is None
        assert data["runner"]["parallel"] == 2 and data["llm_cost_usd"] == 0
        assert all(m["model"] == "offline-script" for m in data["models"].values())
        assert b"Offline stand-in" in client.get(result["report_url"]).data
        assert client.get(result["artifacts"]["fix.patch"]).status_code == 200
        assert not (jobs.jobs[result["id"]].directory / "repo.bundle").exists()
    finally:
        jobs.close()
    assert before == git(repo, "status", "--porcelain") and head == git(repo, "rev-parse", "HEAD")
