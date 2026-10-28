"""Local runner, for building and testing Culprit without credits.

It runs the repo's code on YOUR machine, so only use it with repos you trust
(like the bundled demo). The real product uses Nebius Sandboxes.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

from culprit.runners.base import RunResult, Runner
from culprit.runners.scripts import SCRIPTS


class LocalRunnerError(RuntimeError):
    pass


async def _exec(cmd: list[str], env: dict | None = None) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env=env
    )
    out, _ = await proc.communicate()
    return proc.returncode, out.decode("utf-8", errors="replace")


class LocalRunner(Runner):
    name = "Local machine (trusted repos only)"
    short = "local"

    def __init__(self, parallel: int = 4) -> None:
        super().__init__()
        self.sem = asyncio.Semaphore(parallel)
        self.tmp: Path | None = None
        self.base: Path | None = None
        self.scripts: Path | None = None

    async def prepare(self, bundle: Path, head_sha: str) -> str:
        if shutil.which("sh") is None:
            raise LocalRunnerError("Local mode needs a POSIX shell (macOS, Linux or WSL).")
        code, out = await _exec([sys.executable, "-m", "pytest", "--version"])
        if code != 0:
            raise LocalRunnerError("Local mode needs pytest in this Python: pip install pytest")
        self.tmp = Path(tempfile.mkdtemp(prefix="culprit-local-"))
        self.base = self.tmp / "base"
        code, out = await _exec(["git", "clone", "-q", str(bundle), str(self.base)])
        if code != 0:
            raise LocalRunnerError(f"Could not clone the bundle: {out}")
        self.scripts = self.tmp / "scripts"
        self.scripts.mkdir()
        for name, body in SCRIPTS.items():
            (self.scripts / name).write_text(body, encoding="utf-8")
        self.stats.runs += 1
        return f"local clone ready in {self.tmp}"

    async def run_script(self, script: str, args: list[str], files: dict[str, bytes], timeout: int) -> RunResult:
        assert self.tmp and self.base and self.scripts, "call prepare() first"
        async with self.sem:
            run_dir = Path(tempfile.mkdtemp(dir=self.tmp, prefix="run-"))
            repo, work = run_dir / "repo", run_dir / "work"
            work.mkdir()
            t0 = time.monotonic()
            try:
                code, out = await _exec(["git", "clone", "-q", "--shared", "--no-checkout", str(self.base), str(repo)])
                if code != 0:
                    res = RunResult(-1, out, "", time.monotonic() - t0, error="local clone failed")
                else:
                    for path, data in files.items():
                        rel = path[len("/culprit/"):] if path.startswith("/culprit/") else path.lstrip("/")
                        dest = work / rel
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        dest.write_bytes(data)
                    env = dict(
                        os.environ,
                        CULPRIT_REPO=str(repo),
                        CULPRIT_WORK=str(work),
                        CULPRIT_PYTHON=sys.executable,
                        PYTHONDONTWRITEBYTECODE="1",
                    )
                    proc = await asyncio.create_subprocess_exec(
                        "sh", str(self.scripts / script), *args,
                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env,
                    )
                    try:
                        out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
                        res = RunResult(
                            proc.returncode,
                            out_b.decode("utf-8", errors="replace"),
                            err_b.decode("utf-8", errors="replace"),
                            time.monotonic() - t0,
                        )
                    except asyncio.TimeoutError:
                        proc.kill()
                        await proc.wait()
                        res = RunResult(124, "", "", time.monotonic() - t0)
            finally:
                shutil.rmtree(run_dir, ignore_errors=True)
        self.stats.runs += 1
        self.stats.seconds += res.seconds
        if res.error:
            self.stats.errors += 1
        return res

    async def close(self) -> None:
        if self.tmp:
            shutil.rmtree(self.tmp, ignore_errors=True)
