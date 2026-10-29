"""Failure paths use synthetic credentials and never contact a live service."""

import asyncio
import importlib
import json
import stat
import subprocess
import threading
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from culprit import gitops
from culprit.cli import main
from culprit.config import Settings
from culprit.events import EventLog
from culprit.hunt import _Hunt
from culprit.runners.contree_runner import ContreeRunner, RunnerError
from culprit.tasks import gather, run_blocking


@pytest.mark.parametrize("failure", ["error", "cancel"])
def test_parallel_work_is_drained_before_cleanup(failure):
    async def run():
        started, drained = asyncio.Event(), asyncio.Event()

        async def waiting():
            started.set()
            try:
                await asyncio.Future()
            finally:
                await asyncio.sleep(0)
                drained.set()

        async def failing():
            await started.wait()
            raise RuntimeError("probe failed")

        task = asyncio.create_task(gather(waiting(), failing() if failure == "error" else waiting()))
        if failure == "cancel":
            await started.wait()
            task.cancel()
        with pytest.raises(RuntimeError if failure == "error" else asyncio.CancelledError):
            await task
        assert drained.is_set()

    asyncio.run(run())


def test_hunt_drains_failed_probe_siblings_and_redacts_settings(tmp_path, monkeypatch):
    module = importlib.import_module("culprit.hunt")
    settings = replace(Settings.from_env(), api_key="synthetic-settings-key", project_id="synthetic-settings-project")
    events = EventLog(tmp_path / "events.jsonl")
    state = {"drained": False, "closed": False}

    class Runner:
        name = "test runner"
        stats = SimpleNamespace(details=[], runs=2, seconds=0, errors=1, reported_cost=0)

        async def close(self):
            assert state["drained"]
            state["closed"] = True

    async def prepare(self):
        self.repo, self.bad = tmp_path, "bad"

    async def reproduce(self, ctx):
        self.test_code = "test"

    async def probe(self, sha, **kwargs):
        if sha == "bad":
            await started.wait()
            raise RuntimeError(settings.api_key + " " + settings.project_id)
        started.set()
        try:
            await asyncio.Future()
        finally:
            state["drained"] = True

    monkeypatch.setattr(_Hunt, "prepare", prepare)
    monkeypatch.setattr(_Hunt, "reproduce", reproduce)
    monkeypatch.setattr(_Hunt, "probe", probe)
    monkeypatch.setattr(module.context, "build_context", lambda *args: SimpleNamespace(files={}, picked_by="test"))
    monkeypatch.setattr(gitops, "first_parent_history", lambda *args, **kwargs: ["bad", "waiting", "older"])

    async def run():
        nonlocal started
        started = asyncio.Event()
        h = _Hunt(str(tmp_path), "bug", settings, tmp_path, events, "HEAD", None,
                  Runner(), None, False, False, "contree", False)

        async def search():
            await gather(h.probe("bad"), h.probe("waiting"))

        h.search = search
        return await h.run()

    started = None
    data, code = asyncio.run(run())
    assert code == 1 and not data["ok"] and state["closed"]
    assert events.events[-1]["kind"] == "run_finished"
    for path in tmp_path.iterdir():
        text = path.read_text(encoding="utf-8")
        assert settings.api_key not in text and settings.project_id not in text
    assert (tmp_path / "report.html").is_file()


def test_cancelled_worker_finishes_before_its_resources_are_closed():
    async def run():
        started, release, finished = threading.Event(), threading.Event(), threading.Event()

        def worker():
            started.set()
            assert release.wait(5)
            finished.set()

        task = asyncio.create_task(run_blocking(worker))
        while not started.is_set():
            await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished.is_set()

    asyncio.run(run())


def test_report_write_failure_emits_failed_completion(tmp_path, monkeypatch):
    module = importlib.import_module("culprit.hunt")
    settings = replace(Settings.from_env(), api_key=None)
    events = EventLog()

    def failed(*args):
        raise PermissionError("report directory is read-only")

    monkeypatch.setattr(module, "write_report", failed)
    h = _Hunt(str(tmp_path), "bug", settings, tmp_path, events, "HEAD", None,
              None, None, False, False, "contree", False)
    data, code = asyncio.run(h.run())
    assert code == 1 and not data["ok"]
    assert "Could not write report" in data["reason"]
    assert events.events[-1]["kind"] == "run_finished" and not events.events[-1]["ok"]
    assert any(e["kind"] == "stage" and e["name"] == "report" and e["status"] == "failed"
               for e in events.events)


@pytest.mark.parametrize("operation", ["cancel", "timeout"])
def test_local_script_failure_reaps_process_and_removes_clone(tmp_path, operation):
    from culprit.runners.local_runner import LocalRunner

    runner = LocalRunner()
    runner.tmp = tmp_path / "runner"
    runner.tmp.mkdir()
    runner.base = runner.tmp / "base"
    runner.scripts = runner.tmp / "scripts"
    runner.scripts.mkdir()
    subprocess.run(["git", "init", "-q", str(runner.base)], check=True)
    script = runner.scripts / "waiting.sh"
    script.write_bytes(b'"$CULPRIT_PYTHON" "$CULPRIT_WORK/wait.py"\n')
    ready = tmp_path / "ready"
    code = f"from pathlib import Path\nimport time\nPath({str(ready)!r}).write_text('ready')\ntime.sleep(60)\n"

    async def run():
        task = asyncio.create_task(runner.run_script("waiting.sh", [], {"/culprit/wait.py": code.encode()}, 2))
        try:
            if operation == "cancel":
                for _ in range(500):
                    if ready.exists():
                        break
                    await asyncio.sleep(0.01)
                assert ready.exists()
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 10)
            else:
                result = await asyncio.wait_for(task, 15)
                assert result.exit_code == 124
            assert not list(runner.tmp.glob("run-*"))
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await runner.close()
        assert not runner.tmp.exists()

    asyncio.run(run())


@pytest.mark.parametrize("data", [{"repo": None}, {"models": []}, {"seconds": "invalid"}])
def test_corrupt_report_is_a_clear_failure_without_overwriting(data, tmp_path, capsys):
    original = json.dumps(data)
    (tmp_path / "run.json").write_text(original)
    (tmp_path / "report.html").write_text("existing evidence")
    assert main(["report", str(tmp_path)]) == 1
    assert "Could not rebuild report:" in capsys.readouterr().out
    assert (tmp_path / "run.json").read_text() == original
    assert (tmp_path / "report.html").read_text() == "existing evidence"


def test_git_subprocess_credentials_and_safe_errors(tmp_path, monkeypatch):
    monkeypatch.setenv("NEBIUS_API_KEY", "synthetic-git-key")
    monkeypatch.setenv("NEBIUS_PROJECT_ID", "synthetic-git-project")

    def failed(*args, **kwargs):
        assert "NEBIUS_API_KEY" not in kwargs["env"]
        assert "NEBIUS_PROJECT_ID" not in kwargs["env"]
        return SimpleNamespace(returncode=1, stdout=b"", stderr=b"synthetic-git-key synthetic-git-project")

    monkeypatch.setattr(gitops.subprocess, "run", failed)
    with pytest.raises(gitops.GitError) as caught:
        gitops.git(tmp_path, "show", "synthetic-git-key")
    assert "synthetic-git-key" not in str(caught.value)
    assert "synthetic-git-project" not in str(caught.value)


def test_url_cache_does_not_reuse_different_repository(tmp_path, monkeypatch):
    calls = []

    def clone(cmd, **kwargs):
        calls.append(cmd)
        Path(cmd[-1]).mkdir()
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(gitops.subprocess, "run", clone)
    a = gitops.ensure_local_repo("https://example.invalid/one/project.git", tmp_path)
    b = gitops.ensure_local_repo("https://example.invalid/two/project.git", tmp_path)
    assert a != b and len(calls) == 2
    assert all(cmd[1] == "clone" for cmd in calls)
    assert all("--no-checkout" in cmd and "--template=" in cmd for cmd in calls)


def test_patch_build_ignores_global_hooks_filters_and_git_location(tmp_path, monkeypatch):
    repo = tmp_path / "source"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "a.py").write_bytes(b"original\n")
    (repo / ".gitattributes").write_bytes(b"*.py filter=sentinel diff=sentinel\n")
    gitops.git(repo, "add", "a.py", ".gitattributes")
    gitops.git(repo, "-c", "user.name=test", "-c", "user.email=test@example.com", "commit", "-qm", "base")
    before = gitops.git(repo, "status", "--porcelain")
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    hook = hooks / "post-checkout"
    hook.write_bytes(b'#!/bin/sh\necho invoked > "$HOOK_SENTINEL"\n')
    hook.chmod(0o755)
    sentinel = tmp_path / "hook-ran"
    config = tmp_path / "global-config"
    config.write_text('[core]\n hooksPath = ' + hooks.as_posix() + '\n'
                      '[filter "sentinel"]\n clean = sh ' + hook.as_posix() + '\n smudge = sh ' + hook.as_posix() + '\n'
                      '[diff "sentinel"]\n textconv = sh ' + hook.as_posix() + '\n', encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_DIR", str(repo / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(repo))
    monkeypatch.setenv("GIT_INDEX_FILE", str(repo / ".git/index"))
    monkeypatch.setenv("HOOK_SENTINEL", str(sentinel))
    assert "original" in gitops.diff_of(repo, "HEAD")
    patch, error = gitops.patch_for(repo, "HEAD", files={"a.py": "fixed\n"})
    assert error is None and "+fixed" in patch
    assert not sentinel.exists()
    assert (repo / "a.py").read_bytes() == b"original\n"
    # Status itself may invoke the configured filter; ignore global config here.
    assert gitops.git(repo, "status", "--porcelain", isolated=True) == before


def test_patch_clone_cleanup_handles_readonly_files(tmp_path, monkeypatch):
    clone = tmp_path / "temporary-patch"
    clone.mkdir()
    obj = clone / "readonly-object"
    obj.write_text("object")
    obj.chmod(stat.S_IREAD)
    monkeypatch.setattr(gitops.tempfile, "mkdtemp", lambda **kwargs: str(clone))
    monkeypatch.setattr(gitops, "validate_patch_paths", lambda *args: None)
    monkeypatch.setattr(gitops.subprocess, "run", lambda *args, **kwargs:
                        SimpleNamespace(returncode=1, stderr=b"cannot clone"))
    assert gitops.patch_for(tmp_path, "HEAD") == (None, "cannot clone")
    assert not clone.exists()


def test_patch_cleanup_failure_is_not_silenced(tmp_path, monkeypatch):
    monkeypatch.setattr(gitops.tempfile, "mkdtemp", lambda **kwargs: str(tmp_path))
    monkeypatch.setattr(gitops, "validate_patch_paths", lambda *args: None)
    monkeypatch.setattr(gitops.subprocess, "run", lambda *args, **kwargs:
                        SimpleNamespace(returncode=1, stderr=b"cannot clone"))

    def failed(path):
        raise OSError("could not remove temporary clone")

    monkeypatch.setattr(gitops, "remove_tree", failed)
    with pytest.raises(OSError, match="temporary clone"):
        gitops.patch_for(tmp_path, "HEAD")


def test_sandbox_rejects_project_credential_upload_without_network():
    settings = replace(Settings.from_env(), api_key="synthetic-upload-key", project_id="synthetic-upload-project")
    runner = ContreeRunner(settings)
    with pytest.raises(RunnerError, match="host credential"):
        runner._check_upload({"/culprit/test.py": settings.project_id.encode()})


def test_invalid_cli_argument_does_not_echo_secret(monkeypatch, capsys):
    monkeypatch.setenv("NEBIUS_API_KEY", "synthetic-cli-key")
    with pytest.raises(SystemExit) as caught:
        main(["hunt", "--repo", "missing", "--bug", "bug", "--runner", "synthetic-cli-key"])
    assert caught.value.code == 2
    assert "synthetic-cli-key" not in capsys.readouterr().err


def test_missing_bug_file_is_a_clear_failure(tmp_path, capsys):
    assert main(["hunt", "--repo", str(tmp_path), "--bug-file", str(tmp_path / "missing")]) == 1
    assert "missing" in capsys.readouterr().out


def test_doctor_interrupt_is_a_clear_failure(monkeypatch, capsys):
    module = importlib.import_module("culprit.doctor")

    async def interrupted(settings):
        raise KeyboardInterrupt

    monkeypatch.setattr(module, "doctor", interrupted)
    assert main(["doctor"]) == 1
    assert capsys.readouterr().out.strip() == "Interrupted."


def test_inactive_sandbox_has_explicit_guidance(monkeypatch):
    module = importlib.import_module("culprit.doctor")
    settings = replace(Settings.from_env(), api_key="synthetic-doctor-key", project_id="synthetic-doctor-project")
    monkeypatch.setattr(module, "make_client", lambda settings: (_ for _ in ()).throw(ValueError()))

    async def inactive():
        return False, "Sandbox project inactive"

    monkeypatch.setattr(module, "check_access", inactive)
    output = []
    assert asyncio.run(module.doctor(settings, output.append)) == 1
    assert any("--runner local" in line and "contree@nebius.com" in line for line in output)
