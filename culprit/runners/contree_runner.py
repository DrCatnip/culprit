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
import os
import shlex
import time
from pathlib import Path

from culprit.runners.base import RunResult, Runner
from culprit.runners.scripts import SCRIPTS


class RunnerError(RuntimeError):
    pass


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

    async def prepare(self, bundle: Path, head_sha: str) -> str:
        try:
            from contree_sdk import Contree
        except ImportError as exc:  # pragma: no cover - depends on install
            raise RunnerError("contree-sdk is not installed. Run: pip install contree-sdk") from exc
        if not os.environ.get("NEBIUS_API_KEY") or not os.environ.get("NEBIUS_PROJECT_ID"):
            raise RunnerError("Set NEBIUS_API_KEY and NEBIUS_PROJECT_ID (copy .env.example to .env).")

        self.client = Contree()  # reads NEBIUS_API_KEY and NEBIUS_PROJECT_ID
        base = await self.client.images.oci(self.settings.image)

        files = {"/tmp/repo.bundle": bundle.read_bytes()}
        for name, body in SCRIPTS.items():
            files[f"/culprit/{name}"] = body.encode("utf-8")

        t0 = time.monotonic()
        ready = await base.run(
            shell=f"sh /culprit/setup.sh {shlex.quote(head_sha)}",
            files=files,
            disposable=False,  # keep this state: it's the checkpoint every probe branches from
            timeout=self.settings.setup_timeout,
        )
        out = _text(ready.stdout) + "\n" + _text(ready.stderr)
        self.stats.runs += 1
        self.stats.seconds += time.monotonic() - t0
        if ready.exit_code != 0 or "CULPRIT_READY" not in out:
            tail = "\n".join(out.strip().splitlines()[-20:])
            raise RunnerError(f"Sandbox setup failed (exit {ready.exit_code}).\n{tail}")
        self.ready = ready
        self.checkpoint_id = str(ready.uuid)
        info = next((line for line in out.splitlines() if "CULPRIT_READY" in line), "")
        return f"checkpoint {str(ready.uuid)[:8]} ready ({info.replace('CULPRIT_READY', '').strip()})"

    async def run_script(self, script: str, args: list[str], files: dict[str, bytes], timeout: int) -> RunResult:
        if self.ready is None:
            raise RunnerError("Call prepare() first.")
        cmd = f"sh /culprit/{script} " + " ".join(shlex.quote(a) for a in args)
        async with self.sem:
            t0 = time.monotonic()
            try:
                r = await self.ready.run(
                    shell=cmd,
                    files=files or None,
                    env={"PYTHONDONTWRITEBYTECODE": "1"},
                    disposable=True,  # a throwaway branch of the checkpoint
                    timeout=timeout,
                )
                res = RunResult(r.exit_code, _text(r.stdout), _text(r.stderr), time.monotonic() - t0)
                try:
                    self.stats.reported_cost += float(r.result.cost or 0)
                except Exception:
                    pass
            except Exception as exc:  # network errors, quota, timeouts...
                res = RunResult(-1, "", "", time.monotonic() - t0, error=f"{type(exc).__name__}: {exc}")
        self.stats.runs += 1
        self.stats.seconds += res.seconds
        if res.error:
            self.stats.errors += 1
        return res


async def check_access() -> tuple[bool, str]:
    """Used by `culprit doctor`: can we list images with these credentials?"""
    try:
        from contree_sdk import Contree
    except ImportError:
        return False, "contree-sdk not installed (pip install contree-sdk)"
    try:
        client = Contree()
        images = await client.images()
        return True, f"{len(images)} images visible"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
