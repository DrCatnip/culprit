"""Nebius Sandboxes runner (Token Factory). Every probe runs in its own sandbox branch.

How it uses Sandboxes:
1. Import a base image once (python:3.12-slim by default).
2. Upload the repo as a git bundle plus three small scripts, clone it and install
   dependencies, and save that state with disposable=False. That's our checkpoint.
3. Every probe and every fix check starts from the checkpoint as its own branch
   (disposable=True), so runs are isolated and many run at the same time.
"""

from __future__ import annotations

import asyncio
import shlex
import time
from contextlib import asynccontextmanager
from pathlib import Path

from culprit.config import Settings
from culprit.runners.base import RunResult, Runner
from culprit.runners.scripts import SCRIPTS
from culprit.security import redact, safe_error


class RunnerError(RuntimeError):
    pass


def new_client(settings: Settings):
    """Keep host credentials in SDK authentication, never in guest environments."""
    if not settings.api_key or not settings.project_id:
        raise RunnerError("Set NEBIUS_API_KEY and NEBIUS_PROJECT_ID (copy .env.example to .env).")
    try:
        from contree_sdk import Contree
        from contree_sdk.auth import IAMAuth
        from contree_sdk.config import ContreeConfig
    except ImportError:
        raise RunnerError("contree-sdk is not installed. Run: pip install contree-sdk==0.3.6") from None
    try:
        client = Contree(config=ContreeConfig(auth=IAMAuth(token=settings.api_key, project_id=settings.project_id)))
    except Exception as exc:
        raise RunnerError(f"Could not configure Sandbox client: {safe_error(exc)}.") from None
    if str(client.config.auth.base_url).rstrip("/") != "https://api.tokenfactory.nebius.com/sandboxes":
        raise RunnerError("Sandboxes must use https://api.tokenfactory.nebius.com/sandboxes; check Contree auth configuration.")
    return client


async def close_client(client) -> None:
    # The pinned SDK 0.3.6 has no public close method. These are its HTTP transports.
    cache = client._api._client_per_loop
    failed = False
    for loop, transport in list(cache.items()):
        try:
            await transport.aclose()
        except Exception:
            failed = True
        else:
            cache.pop(loop, None)
    if failed:
        raise RunnerError("Could not close all Sandbox HTTP transports.")


@asynccontextmanager
async def sandbox_client(settings: Settings):
    client = new_client(settings)
    try:
        yield client
    finally:
        await close_client(client)


def _text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


class ContreeRunner(Runner):
    name = "Nebius Sandboxes (Token Factory)"
    short = "Nebius Sandboxes"

    def __init__(self, settings) -> None:
        super().__init__()
        self.settings = settings
        self.sem = asyncio.Semaphore(settings.parallel)
        self.client = None
        self.ready = None
        self.checkpoint_id: str | None = None
        self._active: set[asyncio.Future] = set()
        self._closed = False
        self.cleanup_note = ""

    async def _await(self, operation, timeout: int):
        # Cancelling the SDK await invokes its operation canceller before cleanup.
        task = asyncio.ensure_future(operation)
        self._active.add(task)
        try:
            return await asyncio.wait_for(task, timeout=timeout + 15)
        finally:
            self._active.discard(task)

    def _record(self, script: str, seconds: float, run=None, error: bool = False) -> bool:
        self.stats.runs += 1
        self.stats.seconds += seconds
        self.stats.errors += int(error)
        result = getattr(run, "result", None)
        truncated = bool(getattr(result, "truncated", False))
        self.stats.reported_cost += float(getattr(result, "cost", 0) or 0)
        self.stats.details.append({"script": script, "seconds": seconds, "truncated": truncated, "error": error})
        return truncated

    def _check_upload(self, files: dict[str, bytes]) -> None:
        credentials = (self.settings.api_key, self.settings.project_id)
        if any(value and value.encode("utf-8") in content
               for value in credentials for content in files.values()):
            raise RunnerError("Sandbox upload contains the configured host credential; refusing to upload it.")

    async def prepare(self, bundle: Path, head_sha: str) -> str:
        if self._closed or self.client is not None:
            raise RunnerError("Use a fresh Sandbox runner for each hunt.")
        files = {"/tmp/repo.bundle": bundle.read_bytes()}
        for name, body in SCRIPTS.items():
            files[f"/culprit/{name}"] = body.encode("utf-8")
        self._check_upload(files)
        self.client = new_client(self.settings)
        try:
            base = await self._await(self.client.images.oci(self.settings.image), self.settings.setup_timeout)
        except Exception as exc:
            raise RunnerError(f"Sandbox image import failed: {safe_error(exc)}.") from None
        t0 = time.monotonic()
        try:
            ready = await self._await(base.run(
                shell=f"sh /culprit/setup.sh {shlex.quote(head_sha)}", files=files,
                env={"PYTHONDONTWRITEBYTECODE": "1"}, preserve_env=False,
                disposable=False, timeout=self.settings.setup_timeout,
            ), self.settings.setup_timeout)
        except BaseException as exc:
            self._record("setup.sh", time.monotonic() - t0, error=True)
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise RunnerError(f"Sandbox setup failed: {safe_error(exc)}.") from None
        self._record("setup.sh", time.monotonic() - t0, ready, error=ready.exit_code != 0)
        self.checkpoint_id = str(ready.uuid) if ready.uuid is not None else None
        out = redact(_text(ready.stdout) + "\n" + _text(ready.stderr), (self.settings.api_key or "", self.settings.project_id or ""))
        if ready.exit_code != 0 or "CULPRIT_READY" not in out:
            tail = "\n".join(out.strip().splitlines()[-20:])
            raise RunnerError(f"Sandbox setup failed (exit {ready.exit_code}).\n{tail}")
        self.ready = ready
        if self.checkpoint_id is None:
            raise RunnerError("Sandbox setup did not return a reusable checkpoint.")
        info = next((line for line in out.splitlines() if "CULPRIT_READY" in line), "")
        return f"checkpoint {str(ready.uuid)[:8]} ready ({info.replace('CULPRIT_READY', '').strip()})"

    async def run_script(self, script: str, args: list[str], files: dict[str, bytes], timeout: int) -> RunResult:
        if self.ready is None or self._closed:
            raise RunnerError("Call prepare() first.")
        self._check_upload(files)
        cmd = f"sh /culprit/{script} " + " ".join(shlex.quote(a) for a in args)
        async with self.sem:
            t0 = time.monotonic()
            r = None
            failed = False
            try:
                r = await self._await(self.ready.run(
                    shell=cmd,
                    files=files or None,
                    env={"PYTHONDONTWRITEBYTECODE": "1"},
                    preserve_env=False,
                    disposable=True,  # a throwaway branch of the checkpoint
                    timeout=timeout,
                ), timeout)
                secrets = (self.settings.api_key or "", self.settings.project_id or "")
                res = RunResult(r.exit_code, redact(_text(r.stdout), secrets), redact(_text(r.stderr), secrets), time.monotonic() - t0)
                raw = getattr(getattr(r, "result", None), "_raw", None)
                state = getattr(getattr(raw, "result", None), "state", None)
                if state is not None and state.timed_out:
                    res.exit_code, res.error = 124, "Sandbox execution timed out."
                if script == "probe.sh" and res.exit_code == 1 and getattr(r.result, "truncated", False):
                    res.error = "Sandbox probe output was truncated; failure types cannot be trusted."
                failed = bool(res.error)
            except asyncio.CancelledError:
                failed = True
                raise
            except Exception as exc:  # network errors, quota, timeouts...
                failed = True
                message = safe_error(exc)
                res = RunResult(124 if "timed out" in message else -1, "", "", time.monotonic() - t0, error=message)
            finally:
                self._record(script, time.monotonic() - t0, r, error=failed)
        return res

    async def close(self) -> None:
        self._closed = True
        tasks = list(self._active)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._active.clear()
        self.ready = None
        if self.checkpoint_id:
            self.cleanup_note = "Sandbox checkpoint retained under service policy; SDK 0.3.6 exposes no image-deletion method."
        if self.client is not None:
            await close_client(self.client)
            self.client = None


async def check_access() -> tuple[bool, str]:
    """Used by `culprit doctor`: can we list images with these credentials?"""
    settings = Settings.from_env()
    try:
        async with sandbox_client(settings) as client:
            images = await asyncio.wait_for(client.images(), timeout=20)
        return True, f"{len(images)} images visible"
    except RunnerError as exc:
        return False, str(exc)
    except Exception as exc:
        return False, safe_error(exc)
