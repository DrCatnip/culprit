"""Configuration and connectivity checks; never display credential values."""

from __future__ import annotations

import difflib
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

from culprit.config import Settings, TOKEN_FACTORY_URL
from culprit.events import EventLog
from culprit.llm import LLMError, TokenFactoryLLM, is_nemotron, make_client, validate_endpoint
from culprit.runners.contree_runner import check_access
from culprit.tasks import run_blocking
from culprit.security import redact, safe_error


def closest_model(configured: str, available: list[str]) -> str | None:
    choices = {model.casefold(): model for model in available if is_nemotron(model)}
    if configured.casefold() in choices:
        return choices[configured.casefold()]
    matches = difflib.get_close_matches(configured.casefold(), choices, n=1, cutoff=0.4)
    return choices[matches[0]] if matches else None


async def doctor(settings: Settings, emit: Callable[[str], None] = print) -> int:
    checks: list[bool] = []

    def result(name: str, ok: bool, detail: str) -> None:
        checks.append(ok)
        emit(redact(f"{name}: {'ok' if ok else 'FAILED'} - {detail}", (settings.api_key or "", settings.project_id or "")))

    result("Python", sys.version_info >= (3, 11), sys.version.split()[0] + " (requires 3.11+)")
    if shutil.which("git"):
        try:
            version = await run_blocking(subprocess.run, ["git", "--version"], capture_output=True, text=True)
            result("Git", version.returncode == 0, version.stdout.strip() if version.returncode == 0 else "could not run git")
        except OSError:
            result("Git", False, "could not run git")
    else:
        result("Git", False, "git is missing from PATH")
    emit(".env: " + ("found" if Path(".env").is_file() else "not found; environment variables are supported"))
    emit("NEBIUS_API_KEY: " + ("set" if settings.api_key else "missing"))
    emit("NEBIUS_PROJECT_ID: " + ("set" if settings.project_id else "missing"))
    emit("Token Factory endpoint: " + TOKEN_FACTORY_URL)
    for role, model in settings.models.items():
        emit(redact(f"Configured {role}: {model}", (settings.api_key or "", settings.project_id or "")))
    client = None
    nano_found = False
    try:
        validate_endpoint(settings.base_url)
        if not settings.api_key:
            raise LLMError("NEBIUS_API_KEY is missing; model checks require credentials.")
        client = make_client(settings)
        page = await run_blocking(client.models.list)
        available = [model.id for model in page.data]
        result("Token Factory", True, "model list received")
        for role, model in settings.models.items():
            found = model in available and is_nemotron(model)
            suggestion = closest_model(model, available)
            detail = model if found else f"{model}: not found"
            if not found and suggestion:
                detail += f" - did you mean {suggestion}?"
            result(f"Model {role}", found, detail)
            if role == "nano":
                nano_found = found
        if nano_found:
            llm = TokenFactoryLLM(settings, EventLog(), client=client)
            started = time.monotonic()
            reply = await run_blocking(llm.chat_json, "nano", "Reply with only a JSON object.",
                                            'Return {"ok": true}.', purpose="doctor", max_tokens=128)
            result("Nano JSON call", reply.get("ok") is True, f"JSON response in {time.monotonic() - started:.2f}s")
        else:
            result("Nano JSON call", False, "blocked until the configured Nano model is found")
    except LLMError as exc:
        result("Token Factory", False, str(exc))
    except Exception as exc:
        result("Token Factory", False, safe_error(exc))
    finally:
        if client is not None:
            try:
                await run_blocking(client.close)
            except Exception as exc:
                result("Token Factory cleanup", False, safe_error(exc))
    if settings.api_key and settings.project_id:
        try:
            ok, detail = await check_access()
            result("Sandboxes", ok, detail if ok else detail +
                   "; verify project access with contree auth ls, contact contree@nebius.com if inactive, "
                   "or explicitly use --runner local for a trusted repository")
        except Exception as exc:
            result("Sandboxes", False, safe_error(exc))
    else:
        result("Sandboxes", False, "blocked: NEBIUS_API_KEY and NEBIUS_PROJECT_ID are required")
    return 0 if all(checks) else 1
