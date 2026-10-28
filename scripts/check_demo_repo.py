"""Run the bundled demo's own test suite at every commit, without changing it."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from culprit.demo_repo import is_demo_repo
from culprit.gitops import git
from culprit.security import execution_env


def check_repo(path: Path) -> int:
    path = path.resolve()
    if not is_demo_repo(path):
        raise ValueError("Expected a marked kirana-billing demo repository.")
    commits = git(path, "rev-list", "--reverse", "HEAD").splitlines()
    failures = []
    with tempfile.TemporaryDirectory(prefix="culprit-check-") as tmp:
        clone = Path(tmp) / "repo"
        subprocess.run(["git", "clone", "-q", "--no-hardlinks", str(path), str(clone)], check=True)
        for n, sha in enumerate(commits, 1):
            git(clone, "-c", "advice.detachedHead=false", "checkout", "-q", "--force", sha)
            env = dict(execution_env(), PYTHONDONTWRITEBYTECODE="1", PYTHONPATH=str(clone), PYTEST_DISABLE_PLUGIN_AUTOLOAD="1")
            result = subprocess.run(
                [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
                cwd=clone, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
            )
            if result.returncode:
                failures.append(sha)
                print(f"FAIL commit {n} {sha[:8]}\n{result.stdout}\n{result.stderr}", flush=True)
            elif n % 25 == 0:
                print(f"Checked {n}/{len(commits)} commits", flush=True)
    print(f"{len(commits)} commits checked; {len(failures)} failing commits", flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", type=Path, default=Path("demo/kirana-billing"))
    raise SystemExit(check_repo(parser.parse_args().path))
