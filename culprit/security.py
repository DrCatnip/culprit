"""Keep service credentials out of execution environments and public artifacts."""

from __future__ import annotations

import os
import re
from typing import Any


def _credential_name(name: str) -> bool:
    name = name.upper().replace("-", "_")
    if name == "TOKEN_FACTORY_BASE_URL":
        return False
    return name.startswith(("NEBIUS_", "CONTREE_")) or bool(
        re.search(r"API_?KEY|ACCESS_?KEY|PROJECT_?ID|TOKEN|SECRET|PASSWORD|CREDENTIAL", name)
    )


def redact(text: str, secrets: tuple[str, ...] = ()) -> str:
    values = set(secrets) | {value for name, value in os.environ.items() if _credential_name(name)}
    for value in sorted(filter(None, values), key=len, reverse=True):
        text = text.replace(value, "[REDACTED]")
    text = re.sub(r"(?im)(\b(?:authorization|proxy-authorization|cookie|set-cookie)\s*['\"]?\s*[:=]\s*)[^\r\n]+",
                  r"\1[REDACTED]", text)
    text = re.sub(
        r"(?i)(\b[\w-]*(?:api[_-]?key|access[_-]?key|token|secret|password|credential|project[_-]?id)[\w-]*"
        r"\s*['\"]?\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|[^\s,;}<]+)",
        r"\1[REDACTED]", text)
    return re.sub(r"(https?://)[^/\s@]+@", r"\1[REDACTED]@", text)


def redact_data(value: Any, secrets: tuple[str, ...] = ()) -> Any:
    if isinstance(value, str):
        return redact(value, secrets)
    if isinstance(value, dict):
        return {redact(key, secrets) if isinstance(key, str) else key:
                "[REDACTED]" if isinstance(key, str) and (_credential_name(key) or key.lower() in (
                    "authorization", "proxy-authorization", "cookie", "set-cookie"))
                and key not in ("prompt_tokens", "completion_tokens", "total_tokens")
                else redact_data(item, secrets)
                for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_data(item, secrets) for item in value]
    return value


def execution_env() -> dict[str, str]:
    """Repository code gets normal tooling variables, without service credentials."""
    secrets = [value for name, value in os.environ.items() if _credential_name(name) and value]
    return {name: value for name, value in os.environ.items()
            if not _credential_name(name)
            and not any(secret == value or len(secret) >= 8 and secret in value for secret in secrets)}


def safe_error(exc: BaseException) -> str:
    """Describe service failures without echoing request URLs, headers or bodies."""
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(exc, "status", None)
    if status is None:
        status = getattr(getattr(exc, "response", None), "status_code", None)
    name = type(exc).__name__.lower()
    if isinstance(status, int):
        category = {401: "authentication failed", 403: "access denied", 404: "not found",
                    429: "rate limit or quota exceeded"}.get(status, "service request failed")
        return f"{category} (HTTP {status})"
    if isinstance(exc, TimeoutError) or "timeout" in name or "timedout" in name:
        return "request timed out"
    if "connection" in name or "connect" in name or "network" in name or "transport" in name:
        return "service connection failed"
    if "auth" in name:
        return "authentication failed"
    if isinstance(exc, ValueError):
        return "invalid configuration or response"
    return "service operation failed"
