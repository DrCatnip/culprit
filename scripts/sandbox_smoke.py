"""Live checkpoint plus three disposable branches; requires Nebius credentials."""
import asyncio
import time
from culprit.config import Settings, load_dotenv
from culprit.runners.contree_runner import RunnerError, sandbox_client
from culprit.security import redact, safe_error

async def main() -> int:
    try:
        async with sandbox_client(Settings.from_env()) as client:
            base = await client.images.oci("python:3.12-slim")
            ready = await base.run(shell="python --version && echo hi > /tmp/x", disposable=False, timeout=60, preserve_env=False)
            if ready.exit_code != 0 or ready.uuid is None:
                raise RunnerError("Sandbox smoke setup failed to save a checkpoint.")
            async def probe() -> int:
                started = time.monotonic()
                result = await ready.run(shell="cat /tmp/x && python --version", disposable=True, timeout=60, preserve_env=False)
                print(f"exit={result.exit_code} seconds={time.monotonic() - started:.2f} stdout={redact(str(result.stdout)).strip()}")
                return result.exit_code
            async with asyncio.TaskGroup() as group:
                tasks = [group.create_task(probe()) for _ in range(3)]
            return int(any(task.result() != 0 for task in tasks))
    except Exception as exc:
        print("Sandbox smoke: " + (redact(str(exc)) if isinstance(exc, RunnerError) else safe_error(exc)))
        return 1

if __name__ == "__main__":
    load_dotenv()
    raise SystemExit(asyncio.run(main()))
