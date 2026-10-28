"""Live rendering accepts worker events safely and fits a 100-column terminal."""
import io
import json
from concurrent.futures import ThreadPoolExecutor

from rich.console import Console

from culprit.events import EventLog
from culprit.ui import HuntView, PlainView


def test_plain_is_one_line_per_redacted_event():
    stream = io.StringIO()
    events = EventLog()
    events.on(PlainView(stream))
    events.emit("warn", msg="Authorization: Bearer synthetic-ui-secret\nsecond line")
    events.emit("history", commits=[{"short": "a"}])
    lines = stream.getvalue().splitlines()
    assert len(lines) == 2 and "synthetic-ui-secret" not in stream.getvalue()
    assert json.loads(lines[1].split(" ", 1)[1])["commits"] == 1
    assert "\x1b" not in stream.getvalue()


def test_live_is_locked_and_fits_100_columns():
    stream = io.StringIO()
    console = Console(file=stream, width=100, force_terminal=False)
    view = HuntView("demo", "Bug [red]is literal[/red]", "Local (offline stand-in)", 8, console)
    events = EventLog()
    events.on(view)
    events.emit("history", commits=[{"short": str(i)} for i in range(140)])
    events.emit("stage", name="bisect", status="running", detail="Hunt")

    def work(i):
        events.emit("probe_started", short=str(i), i=i)
        console.render_lines(view, console.options)
        events.emit("probe_finished", short=str(i), i=i, status="pass")

    with ThreadPoolExecutor(max_workers=8) as workers:
        list(workers.map(work, range(140)))
    events.emit("round_finished", round=2, lo=100, hi=110, suspects=10)
    view.finish({"runner": {"runs": 144}, "seconds": 12, "llm_cost_usd": 0.01})
    console.print(view)
    text = stream.getvalue()
    assert view.runs == 144 and len(view.states) == 140
    assert "[red]is literal[/red]" in text and "offline stand-in" in text
    assert "round 2" in text and "144 runner runs" in text
    assert all(len(line) <= 100 for line in text.splitlines())
    assert sum(line.count("▮") for line in text.splitlines()) == 140
    assert "\x1b" not in text


def test_longer_probe_shorthand_colors_the_history_cell():
    console = Console(file=io.StringIO(), width=100)
    view = HuntView("repo", "bug", "local", 8, console)
    view({"kind": "history", "commits": [{"short": "abcdef1"}]})
    view({"kind": "probe_finished", "short": "abcdef12", "status": "pass"})
    segments = [s for line in console.render_lines(view, console.options) for s in line]
    assert any("▮" in s.text and s.style and s.style.color and s.style.color.name == "green" for s in segments)


def test_redirected_cli_automatically_uses_plain(tmp_path, monkeypatch, capsys):
    import importlib
    from culprit.cli import main

    monkeypatch.setattr(HuntView, "__init__", lambda *a, **k: (_ for _ in ()).throw(AssertionError("Live view used for redirected output")))
    monkeypatch.setattr("culprit.demo_repo.is_demo_repo", lambda path: True)

    async def fake_hunt(*args, events, **kwargs):
        events.emit("info", msg="offline stand-in")
        return {"ok": False, "reason": "partial report"}, 1

    monkeypatch.setattr(importlib.import_module("culprit.hunt"), "hunt", fake_hunt)
    assert main(["hunt", "--repo", str(tmp_path), "--bug", "bug", "--offline", "--out", str(tmp_path / "run")]) == 1
    output = capsys.readouterr().out
    assert 'info {"msg": "offline stand-in"}' in output and "\x1b" not in output
