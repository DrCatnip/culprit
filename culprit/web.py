"""Small, optional web adapter for real bundled offline investigations."""

from __future__ import annotations

import asyncio
import atexit
import copy
import os
import re
import tempfile
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from culprit.config import Settings
from culprit.demo_repo import DEMO_BUG, build_demo_repo, is_demo_repo
from culprit.events import EventLog
from culprit.gitops import remove_tree
from culprit.hunt import hunt
from culprit.report import write_report
from culprit.security import redact_data, safe_error

ARTIFACTS = {"report.html", "report.md", "run.json", "fix.patch", "test_culprit_repro.py"}


@dataclass
class DemoJob:
    id: str
    directory: Path
    status: str = "running"
    events: list[dict[str, Any]] = field(default_factory=list)
    result: dict | None = None
    error: str | None = None


class DemoJobs:
    """One bounded worker; no database, external queue, or browser-supplied paths."""

    def __init__(self, runs_root: Path, demo_repo: Path, parallel: int = 2):
        self.runs_root = runs_root.resolve()
        self.demo_repo = demo_repo.resolve()
        self.parallel = max(1, min(int(parallel), 4))
        self.lock = threading.RLock()
        self.jobs: dict[str, DemoJob] = {}
        self.worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="culprit-web")

    def public(self, value: Any) -> Any:
        value = redact_data(value)
        if isinstance(value, str):
            for location in (str(self.demo_repo.parent), str(self.runs_root), tempfile.gettempdir(), str(Path.home())):
                value = value.replace(location, "[demo workspace]")
                value = value.replace(location.replace("\\", "/"), "[demo workspace]")
            return value
        if isinstance(value, dict):
            return {key: self.public(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self.public(item) for item in value]
        return value

    def start(self) -> tuple[DemoJob, bool]:
        with self.lock:
            active = next((job for job in self.jobs.values() if job.status == "running"), None)
            if active:
                return active, False
            while len(self.jobs) >= 5:
                oldest = next(iter(self.jobs))
                expired = self.jobs.pop(oldest)
                if expired.directory.is_relative_to(self.runs_root):
                    remove_tree(expired.directory)
            identity = uuid.uuid4().hex
            job = DemoJob(identity, self.runs_root / identity)
            self.jobs[identity] = job
            self.worker.submit(self.run, job)
            return job, True

    def run(self, job: DemoJob) -> None:
        def receive(event: dict) -> None:
            with self.lock:
                job.events.append(self.public(event))

        try:
            events = EventLog(job.directory / "events.jsonl")
            events.on(receive)
            events.emit("info", msg="Preparing the bundled kirana-billing demo. Scripted model replies; real local Git and pytest.")
            if not is_demo_repo(self.demo_repo):
                build_demo_repo(self.demo_repo)
            settings = replace(Settings.from_env(), api_key=None, project_id=None, parallel=self.parallel)
            data, code = asyncio.run(hunt(self.demo_repo, DEMO_BUG, settings, job.directory,
                                         offline=True, events=events))
            data = self.public(data)
            data["repo"]["source"] = "Bundled kirana-billing demo"
            # Reuse the existing renderer, with public paths and credentials redacted.
            write_report(data, job.directory)
            with self.lock:
                job.result = data
                job.status = "complete" if code == 0 and data.get("ok") else "failed"
                job.error = None if job.status == "complete" else data.get("reason", "Investigation did not complete.")
        except Exception as exc:
            with self.lock:
                job.status = "failed"
                job.error = "The demo could not complete: " + safe_error(exc) + ". Try a new investigation."

    def snapshot(self, identity: str, after: int = 0) -> dict | None:
        with self.lock:
            job = self.jobs.get(identity)
            if not job:
                return None
            counts = {}
            for candidate in (job.result or {}).get("fixes", {}).get("candidates", []):
                output = candidate.get("output", "")
                suite = output.split("=== CULPRIT SUITE ===", 1)[1].split("=== CULPRIT CHANGES ===", 1)[0] if "=== CULPRIT SUITE ===" in output else ""
                found = re.findall(r"\b(\d+) passed(?:\s+in\b|,|\s*$)", suite, re.M)
                counts[candidate["name"]] = int(found[-1]) if found else None
            return copy.deepcopy({"id": job.id, "status": job.status, "error": job.error,
                                  "events": job.events[after:], "cursor": len(job.events),
                                  "result": job.result, "test_counts": counts})

    def close(self) -> None:
        self.worker.shutdown(wait=True, cancel_futures=True)


def create_app(config: dict | None = None):
    """Import Flask only when serving the optional web extra."""
    from flask import Flask, abort, jsonify, request, send_file, url_for
    from werkzeug.exceptions import HTTPException

    assets = Path(__file__).with_name("web_assets")
    app = Flask(__name__, static_folder=str(assets), static_url_path="/static")
    app.config.update(MAX_CONTENT_LENGTH=4096, DEMO_REPO="demo/kirana-billing", RUNS_ROOT="runs/web",
                      DEMO_PARALLEL=os.environ.get("CULPRIT_WEB_PARALLEL", "2"))
    if config:
        app.config.update(config)
    jobs = DemoJobs(Path(app.config["RUNS_ROOT"]), Path(app.config["DEMO_REPO"]), app.config["DEMO_PARALLEL"])
    app.extensions["culprit_jobs"] = jobs
    atexit.register(jobs.close)

    @app.after_request
    def headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        response.headers["Cache-Control"] = "no-store"
        response.headers.setdefault("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; "
                                    "img-src 'self' data:; connect-src 'self'; frame-src 'self'; "
                                    "object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'self'")
        return response

    @app.errorhandler(HTTPException)
    def http_error(exc):
        return jsonify(error={400: "Invalid request.", 404: "Investigation or artifact not found; the service may have restarted.",
                              413: "Request is too large.", 415: "Send an application/json object."}.get(exc.code, exc.name)), exc.code

    @app.errorhandler(Exception)
    def unexpected_error(exc):
        return jsonify(error="The request could not complete: " + safe_error(exc) + "."), 500

    @app.get("/")
    def index():
        return send_file(assets / "index.html")

    @app.get("/health")
    def health():
        return jsonify(status="ok", mode="offline-demo")

    @app.get("/api/demo")
    def demo_config():
        return jsonify(repository="kirana-billing", bug=DEMO_BUG, mode="offline", parallel=jobs.parallel,
                       provenance="Scripted model responses; real local Git/pytest execution.",
                       live_status="G2 BLOCKED — live Nebius credentials unavailable.")

    @app.post("/api/demo")
    def start_demo():
        origin = request.headers.get("Origin")
        if origin and urlsplit(origin).netloc != request.host:
            return jsonify(error="Start the demo from this application's page."), 403
        if not request.is_json:
            abort(415)
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict) or set(payload) - {"mode", "repository", "bug"}:
            abort(400)
        if (payload.get("mode", "offline") != "offline" or
                payload.get("repository", "kirana-billing") != "kirana-billing" or
                payload.get("bug", DEMO_BUG) != DEMO_BUG):
            return jsonify(error="This public demo accepts only the bundled kirana-billing repository and its fixed bug report."), 400
        job, created = jobs.start()
        return jsonify(id=job.id, status_url=url_for("job_status", identity=job.id), reused=not created), 202

    def find_job(identity):
        if not re.fullmatch(r"[0-9a-f]{32}", identity):
            abort(404)
        with jobs.lock:
            job = jobs.jobs.get(identity)
            if job is None:
                abort(404)
            return job

    @app.get("/api/demo/<identity>")
    def job_status(identity):
        find_job(identity)
        try:
            after = int(request.args.get("after", "0"))
        except ValueError:
            abort(400)
        if after < 0:
            abort(400)
        data = jobs.snapshot(identity, after)
        if data is None:
            abort(404)
        data["report_url"] = url_for("report", identity=identity) if data["result"] else None
        with jobs.lock:
            directory = jobs.jobs[identity].directory
            data["artifacts"] = {name: url_for("artifact", identity=identity, filename=name)
                                 for name in sorted(ARTIFACTS) if (directory / name).is_file() and data["result"]}
        return jsonify(data)

    @app.get("/reports/<identity>")
    def report(identity):
        job = find_job(identity)
        if job.result is None or not (job.directory / "report.html").is_file():
            abort(404)
        response = send_file(job.directory / "report.html")
        response.headers["Content-Security-Policy"] = "default-src 'none'; style-src 'unsafe-inline'; "
        response.headers["Content-Security-Policy"] += "base-uri 'none'; form-action 'none'; frame-ancestors 'self'; sandbox"
        return response

    @app.get("/api/demo/<identity>/artifacts/<filename>")
    def artifact(identity, filename):
        job = find_job(identity)
        if filename not in ARTIFACTS or job.result is None or not (job.directory / filename).is_file():
            abort(404)
        return send_file(job.directory / filename, as_attachment=True, download_name=filename)

    return app


def main() -> None:
    create_app().run(host="127.0.0.1", port=int(os.environ.get("PORT", "5000")), threaded=True, debug=False)


if __name__ == "__main__":
    main()
