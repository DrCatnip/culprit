"""Cancellation must close inherited pipes by stopping the test process tree."""

import asyncio
import os
import sys

from culprit.runners.local_runner import _stop_script


def test_stop_script_reaps_child_process():
    child = "import time; print('ready', flush=True); time.sleep(60)"
    parent = f"import subprocess, sys; subprocess.Popen([sys.executable, '-c', {child!r}]).wait()"

    async def run():
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-c", parent, start_new_session=os.name != "nt",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            assert (await asyncio.wait_for(process.stdout.readline(), 10)).strip() == b"ready"
        finally:
            await asyncio.wait_for(_stop_script(process), 10)
        assert process.returncode is not None

    asyncio.run(run())
