"""Command-line interface for Culprit."""

from __future__ import annotations

import argparse


def _hunt_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--runner", choices=("contree", "local"), default="contree")
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--good", metavar="REF")
    parser.add_argument("--parallel", type=int, metavar="N")
    parser.add_argument("--plain", action="store_true")
    parser.add_argument("--out", metavar="DIR")


def main(argv: list[str] | None = None) -> int:
    """Parse a command and return its exit code."""
    parser = argparse.ArgumentParser(
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

    parser.parse_args(argv)
    print("not implemented yet")
    return 1
