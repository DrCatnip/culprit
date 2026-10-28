"""Thread-safe event views. Rendering never changes hunt state."""
from __future__ import annotations

import json
import threading
import time
from collections import deque

from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from culprit.security import redact, redact_data
from culprit.config import PRICES_PER_MILLION

STAGES = ("prepare", "context", "repro", "scout", "bisect", "confirm", "explain", "fix", "report")
COLORS = {"pass": "green", "fail": "red", "skip": "yellow", "testing": "cyan", "untested": "grey70"}


class PlainView:
    def __init__(self, stream):
        self.stream, self.lock = stream, threading.Lock()

    def __call__(self, event):
        event = redact_data(event)
        fields = {k: v for k, v in event.items() if k not in ("t", "kind")}
        if fields.get("model") == "offline-script":
            fields["label"] = fields.get("label", "").replace("(offline)", "(offline stand-in)")
        if event["kind"] == "history":
            fields["commits"] = len(fields["commits"])
        with self.lock:
            print(f"{event['kind']} {json.dumps(fields, ensure_ascii=False)}", file=self.stream, flush=True)


class HuntView:
    def __init__(self, repo, bug, runner, parallel, console=None):
        self.console = console or Console()
        self.lock = threading.RLock()
        self.repo, self.bug, self.runner = map(redact, (str(repo), bug, runner))
        self.parallel = parallel
        self.started = time.monotonic()
        self.stages, self.models, self.states = {}, {}, {}
        self.commits, self.logs = [], deque(maxlen=5)
        self.lo, self.hi, self.round = -1, -1, 0
        self.runs, self.cost, self.finished = 0, 0.0, False
        self.seconds = 0.0
        self.live = Live(self, console=self.console, refresh_per_second=8, vertical_overflow="visible")

    def __enter__(self):
        self.live.start()
        return self

    def __exit__(self, *args):
        self.live.stop()

    def __call__(self, event):
        e = redact_data(event)
        with self.lock:
            kind = e["kind"]
            if kind == "stage":
                self.stages[e["name"]] = (e["status"], e.get("detail", ""))
            elif kind == "history":
                self.commits = e["commits"]
                self.hi = len(self.commits) - 1
            elif kind in ("round_started", "round_finished"):
                self.round, self.lo, self.hi = e["round"], e["lo"], e["hi"]
            elif kind in ("probe_started", "probe_finished"):
                short = e.get("short")
                self.states[short] = "testing" if kind == "probe_started" else e.get("status", "skip")
                if kind == "probe_finished":
                    self.runs += 1
            elif kind.startswith("model_"):
                label = e.get("label", e["role"])
                if e.get("model") == "offline-script":
                    label = label.replace("(offline)", "(offline stand-in)")
                self.models[(e["role"], e.get("purpose", ""))] = (label, e.get("purpose", ""),
                                          "running" if kind == "model_started" else f'{e.get("seconds", 0):.1f} s · {"done" if e.get("ok") else "failed"}')
                if kind == "model_finished" and e.get("model") != "offline-script":
                    pin, pout = PRICES_PER_MILLION.get(e["role"], (0, 0))
                    self.cost += (e.get("prompt_tokens", 0) * pin + e.get("completion_tokens", 0) * pout) / 1e6
            elif kind == "run_finished":
                self.finished, self.seconds = True, e.get("seconds", 0)
            if kind in ("warn", "info", "candidate_finished", "round_finished", "culprit_found", "run_finished"):
                msg = e.get("msg") or e.get("reason") or json.dumps({k: v for k, v in e.items() if k not in ("t", "kind")}, ensure_ascii=False)
                self.logs.append(f"{kind}: {msg}"[:180])

    def finish(self, data):
        with self.lock:
            self.runs = data.get("runner", {}).get("runs", self.runs)
            self.cost = data.get("llm_cost_usd", 0)
            self.seconds, self.finished = data.get("seconds", 0), True

    def __rich_console__(self, console, options):
        with self.lock:
            header = Panel(Text(f"Bug: {self.bug}"), title=Text(f"Culprit · {self.repo} · {self.runner} · {self.parallel} at a time"), border_style="green")
            stages = Table.grid(padding=(0, 2), expand=True)
            stages.add_column(width=12)
            stages.add_column()
            for name in STAGES:
                status, detail = self.stages.get(name, ("pending", ""))
                symbol = {"done": "✓", "running": "●", "failed": "✗", "skipped": "–"}.get(status, "·")
                stages.add_row(Text(f"{symbol} {name}", style="red" if status == "failed" else "green" if status == "done" else "cyan" if status == "running" else "dim"), Text(f"{status} {detail}"))
            history = Text(f"History · oldest → newest ({len(self.commits)} commits) · round {self.round}\n")
            for i, c in enumerate(self.commits):
                short = c.get("short", "")
                status = self.states.get(short) or next((v for k, v in self.states.items()
                                                        if short and k and k.startswith(short)), "untested")
                history.append("▮", style=COLORS.get(status, "grey70") + (" dim" if i <= self.lo or i > self.hi else ""))
                if (i + 1) % max(1, options.max_width) == 0:
                    history.append("\n")
            legend = Text("pass green · fail red · skip yellow · testing cyan · ruled out dim")
            models = Text("Models\n", style="bold")
            for role in ("nano", "super", "ultra"):
                values = [v for (r, _), v in self.models.items() if r == role]
                models.append(f"{role}: " + ("; ".join(' · '.join(v) for v in values) if values else "pending") + "\n", style="default")
            log = Text("Log\n" + "\n".join(self.logs), overflow="ellipsis")
            elapsed = self.seconds if self.finished else time.monotonic() - self.started
            totals = Text(f"{elapsed:.1f} s · {self.runs} {'runner runs' if self.finished else 'completed probes'} · ${self.cost:.5f} model cost" + ("" if self.finished else " (estimate from finished calls; retries finalized at end)"))
            renderable = Group(header, stages, history, legend, models, log, totals)
        yield renderable
