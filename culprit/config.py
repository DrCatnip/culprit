"""Settings, model IDs and prices.

Everything can be overridden with environment variables or a .env file in the
folder you run Culprit from.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

TOKEN_FACTORY_URL = "https://api.tokenfactory.nebius.com/v1/"

# NVIDIA Nemotron 3 models on Nebius Token Factory. IDs are case-sensitive.
# Run `culprit doctor` to check them against the live model list.
DEFAULT_MODELS = {
    "nano": "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B",
    "super": "nvidia/nemotron-3-super-120b-a12b",
    "ultra": "nvidia/Nemotron-3-Ultra-550b-a55b",
}

MODEL_LABELS = {
    "nano": "Nemotron 3 Nano",
    "super": "Nemotron 3 Super",
    "ultra": "Nemotron 3 Ultra",
}

# USD per 1M tokens (input, output). Nebius list prices seen on public price
# trackers in October 2026. Used only for the cost estimate in reports.
PRICES_PER_MILLION = {
    "nano": (0.06, 0.24),
    "super": (0.30, 0.90),
    "ultra": (1.00, 3.00),
}


def load_dotenv(path: str | os.PathLike = ".env") -> None:
    """Load KEY=VALUE lines from a .env file without overriding real env vars."""
    p = Path(path)
    if not p.is_file():
        return
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        value = value.strip()
        if value[:1] in ('"', "'") and value[-1:] == value[:1]:
            value = value[1:-1]
        elif " #" in value:  # inline comment after an unquoted value
            value = value.split(" #", 1)[0].rstrip()
        if key and key not in os.environ:
            os.environ[key] = value


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


@dataclass
class Settings:
    api_key: str | None
    project_id: str | None
    base_url: str
    models: dict[str, str]
    image: str
    parallel: int
    probe_timeout: int
    setup_timeout: int
    max_test_attempts: int
    runs_dir: Path
    cache_dir: Path

    @classmethod
    def from_env(cls) -> "Settings":
        models = {
            "nano": os.environ.get("CULPRIT_MODEL_NANO", DEFAULT_MODELS["nano"]),
            "super": os.environ.get("CULPRIT_MODEL_SUPER", DEFAULT_MODELS["super"]),
            "ultra": os.environ.get("CULPRIT_MODEL_ULTRA", DEFAULT_MODELS["ultra"]),
        }
        return cls(
            api_key=os.environ.get("NEBIUS_API_KEY") or None,
            project_id=os.environ.get("NEBIUS_PROJECT_ID") or None,
            base_url=os.environ.get("TOKEN_FACTORY_BASE_URL", TOKEN_FACTORY_URL),
            models=models,
            image=os.environ.get("CULPRIT_IMAGE", "python:3.12-slim"),
            parallel=max(1, min(_int_env("CULPRIT_PARALLEL", 8), 40)),
            probe_timeout=_int_env("CULPRIT_PROBE_TIMEOUT", 300),
            setup_timeout=_int_env("CULPRIT_SETUP_TIMEOUT", 900),
            max_test_attempts=max(1, _int_env("CULPRIT_MAX_TEST_ATTEMPTS", 3)),
            runs_dir=Path(os.environ.get("CULPRIT_RUNS_DIR", "runs")),
            cache_dir=Path(os.environ.get("CULPRIT_CACHE_DIR", Path.home() / ".cache" / "culprit")),
        )
