"""Command-line interface for Culprit."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from contextlib import nullcontext
from datetime import datetime
from pathlib import Path

from culprit.config import Settings, load_dotenv
from culprit.security import redact


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        super().error(redact(message))


async def _hunt_command(args: argparse.Namespace) -> int:
    from culprit.demo_repo import DEMO_BUG, build_demo_repo, is_demo_repo
    from culprit.events import EventLog
    from culprit.hunt import hunt
    from culprit.ui import HuntView, PlainView

    if args.command == "demo":
        repo = Path("demo/kirana-billing")
        if not is_demo_repo(repo):
            repo = build_demo_repo(repo)
        bug = DEMO_BUG
    else:
        repo = args.repo
        if args.offline and not is_demo_repo(repo):
            raise ValueError("Offline mode only works with the bundled demo repo (run: culprit demo-repo).")
        bug = args.bug if args.bug is not None else Path(args.bug_file).read_text(encoding="utf-8")
    settings = Settings.from_env()
    if args.parallel is not None:
        if not 1 <= args.parallel <= 40:
            raise ValueError("--parallel must be between 1 and 40.")
        settings.parallel = args.parallel
    import re
    name = re.sub(r"[^A-Za-z0-9_.-]", "_", str(repo).rstrip("/\\").replace("\\", "/").split("/")[-1]).removesuffix(".git") or "repo"
    out_dir = Path(args.out) if args.out else settings.runs_dir / f"{datetime.now():%Y%m%d-%H%M%S}-{name}"
    if args.out is None and out_dir.exists():
        out_dir = out_dir.with_name(out_dir.name + f"-{datetime.now():%f}")
    events = EventLog(out_dir / "events.jsonl")
    view = (PlainView(sys.stdout) if args.plain or not sys.stdout.isatty() else
            HuntView(repo, bug, "Local (offline stand-in)" if args.offline else args.runner, settings.parallel))
    events.on(view)
    with view if isinstance(view, HuntView) else nullcontext():
        data, code = await hunt(
            repo, bug, settings, out_dir, bad=getattr(args, "bad", "HEAD"), good=args.good,
            offline=args.offline, no_fix=getattr(args, "no_fix", False), events=events,
            runner_kind=args.runner, trust_repo=getattr(args, "i_trust_this_repo", False),
        )
        if isinstance(view, HuntView):
            view.finish(data)
    if data["ok"]:
        culprit = data["culprit"]["commit"]
        print(f"Culprit: {culprit['short']} {culprit['subject']}")
        print(data["explanation"].get("headline", ""))
        print(f"Chosen fix: {data['fixes']['chosen'] or 'none'}")
    else:
        print(data["reason"])
    print(redact(f"Report: {out_dir / 'report.html'}"))
    return code


def _hunt_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--runner", choices=("contree", "local"), default="contree")
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--good", metavar="REF")
    parser.add_argument("--parallel", type=int, metavar="N")
    parser.add_argument("--plain", action="store_true")
    parser.add_argument("--out", metavar="DIR")


def _main(argv: list[str] | None = None) -> int:
    """Parse a command and return its exit code."""
    parser = _Parser(
        prog="culprit", description="Find the commit that caused a bug."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor", help="Check configuration and service access.")

    demo_repo = commands.add_parser("demo-repo", help="Create the bundled demo repository.")
    demo_repo.add_argument("--path", default="demo/kirana-billing")
    demo_repo.add_argument("--force", action="store_true")

    demo = commands.add_parser("demo", help="Hunt the bug in the bundled demo repository.")
    _hunt_options(demo)

    hunt = commands.add_parser("hunt", help="Find the commit that introduced a reported bug.")
    hunt.add_argument("--repo", required=True, metavar="PATH_OR_URL")
    bug = hunt.add_mutually_exclusive_group(required=True)
    bug.add_argument("--bug", metavar="TEXT")
    bug.add_argument("--bug-file", metavar="FILE")
    hunt.add_argument("--bad", default="HEAD", metavar="REF")
    _hunt_options(hunt)
    hunt.add_argument("--i-trust-this-repo", action="store_true")
    hunt.add_argument("--no-fix", action="store_true")

    report = commands.add_parser("report", help="Rebuild reports from a saved run.")
    report.add_argument("run_dir", metavar="RUN_DIR")

    load_dotenv()
    args = parser.parse_args(argv)
    if args.command == "doctor":
        from culprit.doctor import doctor

        return asyncio.run(doctor(Settings.from_env()))
    if args.command == "demo-repo":
        from culprit.demo_repo import build_demo_repo

        try:
            path = build_demo_repo(args.path, args.force)
        except (OSError, ValueError, RuntimeError) as exc:
            print(redact(str(exc)))
            return 1
        print(redact(f"Demo repository ready: {path}"))
        return 0
    if args.command in ("demo", "hunt"):
        if args.command == "hunt" and args.runner == "local" and not args.i_trust_this_repo:
            from culprit.demo_repo import is_demo_repo

            if not is_demo_repo(args.repo):
                print("Local execution of this repository requires --i-trust-this-repo.")
                return 1
        try:
            return asyncio.run(_hunt_command(args))
        except (OSError, ValueError, RuntimeError) as exc:
            print(redact(str(exc)))
            return 1
        except KeyboardInterrupt:
            print("Interrupted.")
            return 1
    if args.command == "report":
        from culprit.report import write_report

        try:
            run_dir = Path(args.run_dir)
            data = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("run.json must contain an object")
            write_report(data, run_dir)
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
            print(redact(f"Could not rebuild report: {exc}"))
            return 1
        print(redact(f"Report: {run_dir / 'report.html'}"))
        return 0
    return 1


def main(argv: list[str] | None = None) -> int:
    """Return a useful error without a traceback for configuration or interruption."""
    try:
        return _main(argv)
    except KeyboardInterrupt:
        print("Interrupted.")
        return 1
    except (OSError, ValueError, RuntimeError) as exc:
        print(redact(str(exc)))
        return 1
