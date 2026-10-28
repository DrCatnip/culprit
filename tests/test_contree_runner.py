"""Contree SDK protocol, concurrency and cleanup tests with mocked remote operations."""

import asyncio
import importlib
from dataclasses import replace
from types import SimpleNamespace

import pytest

from culprit.config import Settings
from culprit.runners.base import Status, classify
from culprit.runners.contree_runner import ContreeRunner, RunnerError, check_access, close_client, new_client

module = importlib.import_module("culprit.runners.contree_runner")


def settings():
    return replace(Settings.from_env(), api_key="sandbox-key-test-only", project_id="sandbox-project-test-only", parallel=2)


class RemoteMock:
    """Protocol mock only: no remote execution is claimed by these tests."""

    def __init__(self):
        self.calls = []
        self.active = self.maximum = 0
        self.closed = False
        self.cancelled = False
        self.block = False
        self.setup_code = 0
        self.probe_code = 0
        self.truncated = False
        self.timed_out = False
        self.exception = None
        self.started = None
        self.client = SimpleNamespace(
            images=SimpleNamespace(oci=self.oci),
            _api=SimpleNamespace(_client_per_loop={"test-loop": SimpleNamespace(aclose=self.close)}),
        )

    async def close(self):
        self.closed = True

    async def oci(self, image):
        assert image == "python:3.12-slim"
        return self.image("base")

    def image(self, origin):
        def run(**kwargs):
            self.calls.append((origin, kwargs))

            async def execute():
                setup = kwargs["shell"].startswith("sh /culprit/setup.sh")
                if not setup:
                    self.active += 1
                    self.maximum = max(self.maximum, self.active)
                try:
                    if self.exception is not None and not setup:
                        raise self.exception
                    if not setup and self.block:
                        self.started.set()
                        try:
                            await asyncio.Future()
                        finally:
                            self.cancelled = True
                    await asyncio.sleep(0)
                    result = self.image("checkpoint")
                    result.uuid = "checkpoint-uuid" if setup else None
                    result.exit_code = self.setup_code if setup else self.probe_code
                    result.stdout = "CULPRIT_READY abc Python 3.12" if setup else "FAILED test_repro.py::test_bug - AssertionError: got 42"
                    if "verify.sh" in kwargs["shell"]:
                        result.stdout = "=== CULPRIT CODES 0 0 ===\n=== CULPRIT REPRO ===\npass\n=== CULPRIT SUITE ===\npass\n=== CULPRIT END ==="
                    result.stderr = ""
                    result.result = SimpleNamespace(cost=0.25, truncated=self.truncated,
                        _raw=SimpleNamespace(result=SimpleNamespace(state=SimpleNamespace(timed_out=self.timed_out))))
                    return result
                finally:
                    if not setup:
                        self.active -= 1

            return execute()

        return SimpleNamespace(run=run)


def prepare_mock(tmp_path, monkeypatch):
    remote = RemoteMock()
    monkeypatch.setattr(module, "new_client", lambda settings: remote.client)
    bundle = tmp_path / "repo.bundle"
    bundle.write_bytes(b"mock-git-bundle")
    return remote, bundle, ContreeRunner(settings())


def test_sdk_auth_is_scoped_and_endpoint_is_nebius(monkeypatch):
    monkeypatch.setattr("contree_sdk.auth.read_ini_profile", lambda: None)
    config = settings()
    client = new_client(config)
    assert client.config.auth.token == config.api_key
    assert client.config.auth.project_id == config.project_id
    assert str(client.config.auth.base_url).rstrip("/") == "https://api.tokenfactory.nebius.com/sandboxes"
    assert not client._api._client_per_loop
    asyncio.run(close_client(client))


def test_sdk_profile_cannot_redirect_credentials(monkeypatch):
    monkeypatch.setattr("contree_sdk.auth.read_ini_profile", lambda: SimpleNamespace(url="https://other.invalid", token=None, project=None))
    with pytest.raises(RunnerError, match="Sandboxes must use"):
        new_client(settings())


def test_missing_credentials_fail_without_sdk_construction(monkeypatch):
    monkeypatch.setattr("contree_sdk.Contree", lambda **kwargs: pytest.fail("No SDK construction without credentials"))
    with pytest.raises(RunnerError, match="NEBIUS_API_KEY and NEBIUS_PROJECT_ID"):
        new_client(replace(settings(), api_key=None))


def test_checkpoint_upload_disposable_branches_and_parallel_limit(tmp_path, monkeypatch):
    remote, bundle, runner = prepare_mock(tmp_path, monkeypatch)

    async def run():
        await runner.prepare(bundle, "abc123")
        prepared = runner.ready
        results = await asyncio.gather(*(runner.probe("abc123", "def test_ok(): pass", False, 10) for _ in range(6)))
        assert runner.ready is prepared
        verified = await runner.verify("abc123", "def test_ok(): pass", 10, patch_files={"calc.py": "fixed"})
        assert verified.passed
        await runner.close()
        return results

    results = asyncio.run(run())
    assert all(classify(result)[0] == Status.PASS for result in results)
    assert remote.maximum == 2 and remote.closed
    assert runner.checkpoint_id == "checkpoint-uuid" and "retained" in runner.cleanup_note
    origin, setup = remote.calls[0]
    assert origin == "base" and setup["disposable"] is False
    assert set(setup["files"]) == {"/tmp/repo.bundle", "/culprit/setup.sh", "/culprit/probe.sh", "/culprit/verify.sh"}
    for origin, call in remote.calls[1:]:
        assert origin == "checkpoint" and call["disposable"] is True
        assert call["preserve_env"] is False and call["env"] == {"PYTHONDONTWRITEBYTECODE": "1"}
        assert settings().api_key not in str(call)
    assert runner.stats.runs == 8 and runner.stats.reported_cost == 2.0
    assert runner.stats.errors == 0 and not runner._active


@pytest.mark.parametrize("kind", ["timeout", "forbidden", "setup", "truncation", "process_timeout"])
def test_errors_are_safe_and_cannot_be_passing_probes(tmp_path, monkeypatch, kind):
    from contree_sdk.sdk.exceptions import ForbiddenError

    remote, bundle, runner = prepare_mock(tmp_path, monkeypatch)
    if kind == "setup":
        remote.setup_code = 7

    async def run():
        try:
            if kind == "setup":
                with pytest.raises(RunnerError, match="exit 7"):
                    await runner.prepare(bundle, "abc")
                return None
            await runner.prepare(bundle, "abc")
            if kind == "timeout":
                remote.exception = TimeoutError("sandbox-key-test-only")
            elif kind == "forbidden":
                remote.exception = ForbiddenError(error="sandbox-key-test-only")
            elif kind == "truncation":
                remote.truncated = True
                remote.probe_code = 1
            elif kind == "process_timeout":
                remote.timed_out = True
            return await runner.probe("abc", "def test_ok(): pass", False, 10)
        finally:
            await runner.close()

    result = asyncio.run(run())
    assert remote.closed
    if result is not None:
        assert classify(result)[0] == Status.SKIP
        assert "sandbox-key-test-only" not in str(result)
        if kind == "forbidden":
            assert "HTTP 403" in result.error
    assert runner.stats.errors >= 1


def test_close_cancels_operations_and_releases_transports(tmp_path, monkeypatch):
    remote, bundle, runner = prepare_mock(tmp_path, monkeypatch)

    async def run():
        remote.started = asyncio.Event()
        await runner.prepare(bundle, "abc")
        remote.block = True
        task = asyncio.create_task(runner.probe("abc", "test", False, 10))
        await remote.started.wait()
        await runner.close()
        with pytest.raises(asyncio.CancelledError):
            await task
        await runner.close()

    asyncio.run(run())
    assert remote.cancelled and remote.closed and not runner._active


def test_secret_upload_is_rejected_before_sdk_work(tmp_path, monkeypatch):
    remote, bundle, runner = prepare_mock(tmp_path, monkeypatch)
    bundle.write_bytes(settings().api_key.encode())
    with pytest.raises(RunnerError, match="host credential"):
        asyncio.run(runner.prepare(bundle, "abc"))
    assert not remote.calls and runner.client is None


def test_transport_cleanup_failure_is_explicit():
    async def failed():
        raise RuntimeError("cleanup secret")

    client = SimpleNamespace(_api=SimpleNamespace(_client_per_loop={"loop": SimpleNamespace(aclose=failed)}))
    with pytest.raises(RunnerError, match="Could not close all") as caught:
        asyncio.run(close_client(client))
    assert "cleanup secret" not in str(caught.value)


def test_access_check_closes_its_client(monkeypatch):
    closed = []

    async def images():
        return []

    async def close():
        closed.append(True)

    client = SimpleNamespace(images=images, _api=SimpleNamespace(_client_per_loop={"loop": SimpleNamespace(aclose=close)}))
    monkeypatch.setattr(module, "new_client", lambda settings: client)
    assert asyncio.run(check_access()) == (True, "0 images visible")
    assert closed == [True]


def test_truncated_verification_still_uses_codes_first(tmp_path, monkeypatch):
    remote, bundle, runner = prepare_mock(tmp_path, monkeypatch)

    async def run():
        await runner.prepare(bundle, "abc")
        remote.truncated = True
        try:
            result = await runner.verify("abc", "test", 10, patch_files={"calc.py": "fixed"})
            assert result.passed and result.repro_code == 0 and result.suite_code == 0
        finally:
            await runner.close()

    asyncio.run(run())
    assert runner.stats.details[-1]["truncated"]


def test_pinned_sdk_cancels_unfinished_operation(monkeypatch):
    """Run the installed SDK's canceller, mocking only its service endpoints."""
    from uuid import UUID

    monkeypatch.setattr("contree_sdk.auth.read_ini_profile", lambda: None)
    cancelled = []

    async def run():
        started = asyncio.Event()
        client = new_client(settings())
        image = await client.images.use(UUID("00000000-0000-0000-0000-000000000001"))
        operation = UUID("00000000-0000-0000-0000-000000000002")

        async def start(request):
            assert request.disposable is True
            assert settings().api_key not in str(request.env)
            return operation

        async def status(operation_id):
            started.set()
            return None

        async def cancel(operation_id):
            cancelled.append(operation_id)

        monkeypatch.setattr(client, "_start_operation", start)
        monkeypatch.setattr(client._api, "get_operation_status", status)
        monkeypatch.setattr(client._api, "cancel_operation", cancel)
        runner = ContreeRunner(settings())
        runner.client = client
        task = asyncio.create_task(runner._await(image.run(shell="true", disposable=True), 30))
        await asyncio.wait_for(started.wait(), 5)
        await runner.close()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cancelled == [operation] and not runner._active

    asyncio.run(run())
