"""Parallel bisection: test k commits at once instead of one at a time.

Commits are indexed oldest to newest: index -1 is the known good commit and
index n-1 is the known bad one. We keep `lo` (latest known good) and `hi`
(earliest known bad). Each round tests up to k commits spread evenly between
them, so the unknown range shrinks about (k+1) times per round instead of 2.
With k = 8, 150 commits take about 3 rounds; plain `git bisect` takes about 8.
"""

from __future__ import annotations

import math

from culprit.runners.base import Status


def pick_probes(lo: int, hi: int, k: int, skipped: set[int] | frozenset[int] = frozenset()) -> list[int]:
    """Up to k indexes strictly between lo and hi, evenly spaced, avoiding skipped ones."""
    candidates = [i for i in range(lo + 1, hi) if i not in skipped]
    if not candidates or k <= 0:
        return []
    if len(candidates) <= k:
        return candidates
    picks = []
    for j in range(1, k + 1):
        pos = math.floor(j * (len(candidates) + 1) / (k + 1) + 0.5) - 1
        picks.append(candidates[min(max(pos, 0), len(candidates) - 1)])
    return sorted(set(picks))


def update_bounds(lo: int, hi: int, results: dict[int, Status]) -> tuple[int, int, bool]:
    """Move lo up to the newest PASS and hi down to the oldest FAIL.

    Returns (lo, hi, inconsistent). `inconsistent` is True when a commit newer
    than a failing one passes, which means the bug comes and goes (or the test
    is flaky) and the answer needs a human look.
    """
    fails = [i for i, s in results.items() if s == Status.FAIL and lo < i < hi]
    new_hi = min([hi, *fails])
    passes = [i for i, s in results.items() if s == Status.PASS and lo < i < new_hi]
    new_lo = max([lo, *passes])
    inconsistent = any(s == Status.PASS and i > new_hi for i, s in results.items())
    return new_lo, new_hi, inconsistent


def expected_rounds(n_commits: int, k: int) -> int:
    """Worst-case rounds to find the first bad commit among n_commits (last one known bad)."""
    unknown = max(n_commits - 1, 0)
    rounds = 0
    while unknown > 0:
        unknown = 0 if unknown <= k else math.ceil((unknown - k) / (k + 1))
        rounds += 1
    return rounds
