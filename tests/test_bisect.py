"""Every possible boundary, plus skips and histories that are not monotonic."""

import pytest

from culprit.bisect import expected_rounds, pick_probes, update_bounds
from culprit.runners.base import Status


@pytest.mark.parametrize("parallel,limit", [(8, 3), (1, 8)])
def test_all_boundaries(parallel, limit):
    assert expected_rounds(140, parallel) == limit
    for culprit in range(140):
        lo, hi, rounds = -1, 139, 0
        while hi > lo + 1:
            probes = pick_probes(lo, hi, parallel)
            assert probes and len(probes) <= parallel
            lo, hi, inconsistent = update_bounds(lo, hi, {
                i: Status.FAIL if i >= culprit else Status.PASS for i in probes
            })
            assert not inconsistent
            rounds += 1
        assert hi == culprit and rounds <= limit


def test_skipped_commits_leave_ambiguous_boundary():
    skipped = {4, 5, 6}
    lo, hi = 3, 7
    assert pick_probes(lo, hi, 8, skipped) == []
    assert update_bounds(lo, hi, {5: Status.SKIP}) == (3, 7, False)
    assert not set(pick_probes(-1, 20, 8, skipped)) & skipped


def test_inconsistent_results():
    assert update_bounds(-1, 20, {4: Status.FAIL, 8: Status.PASS}) == (-1, 4, True)
