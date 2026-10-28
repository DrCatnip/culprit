"""A tiny event log. The live terminal view and the HTML report both read it."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Callable

from culprit.security import redact_data


class EventLog:
    def __init__(self, path: Path | None = None):
        self.path = path
        self.events: list[dict[str, Any]] = []
        self._listeners: list[Callable[[dict[str, Any]], None]] = []
        self._lock = threading.Lock()
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("", encoding="utf-8")

    def on(self, listener: Callable[[dict[str, Any]], None]) -> None:
        self._listeners.append(listener)

    def emit(self, kind: str, **data: Any) -> dict[str, Any]:
        event = redact_data({"t": round(time.time(), 3), "kind": kind, **data})
        with self._lock:
            self.events.append(event)
            if self.path is not None:
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(event, default=str) + "\n")
        for listener in list(self._listeners):
            try:
                listener(event)
            except Exception:  # a broken view must never stop the hunt
                pass
        return event
