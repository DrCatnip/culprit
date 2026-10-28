"""Smoke tests for the head-start code: imports, bisect maths, result parsing,
and a real local-runner run (probe, verify a fix, verify a revert, build a patch)."""

import asyncio
import importlib
import subprocess
from pathlib import Path

import pytest

from culprit.bisect import expected_rounds, pick_probes, update_bounds
from culprit.llm import extract_json
from culprit.runners.base import RunResult, Status, classify, failure_types

MODULES = ["culprit.config", "culprit.events", "culprit.gitops", "culprit.bisect", "culprit.llm",
           "culprit.prompts", "culprit.context", "culprit.runners", "culprit.runners.base",
           "culprit.runners.scripts", "culprit.runners.contree_runner", "culprit.runners.local_runner"]


@pytest.mark.parametrize("name", MODULES)
def test_imports(name):
    importlib.import_module(name)


def _simulate(n, culprit, k):
    lo, hi, rounds = -1, n - 1, 0
    while hi - lo > 1:
        probes = pick_probes(lo, hi, k)
        results = {i: Status.FAIL if i >= culprit else Status.PASS for i in probes}
        lo, hi, _ = update_bounds(lo, hi, results)
        rounds += 1
    return hi, rounds


def test_bisect_finds_every_position():
    assert expected_rounds(140, 8) == 3 and expected_rounds(140, 1) == 8
    for c in range(140):
        assert _simulate(140, c, 8)[0] == c and _simulate(140, c, 8)[1] <= 3
        assert _simulate(140, c, 1)[0] == c and _simulate(140, c, 1)[1] <= 8


def test_extract_json():
    assert extract_json('<think>{"x": 1}</think>\n```json\n{"files": ["a.py"]}\n```') == {"files": ["a.py"]}
    assert extract_json('Sure! {"test_code": "x = {1: 2}", "why": "w"} done')["why"] == "w"


def test_classify():
    out = "FAILED /culprit/test_culprit_repro.py::test_x - assert 42 == 43\n1 failed in 0.05s"
    assert classify(RunResult(1, out))[0] == Status.FAIL
    assert failure_types(out) == {"AssertionError"}
    other = "FAILED t.py::t - ImportError: cannot import name 'to_paise'"
    assert classify(RunResult(1, other), {"AssertionError"})[0] == Status.SKIP
    assert [classify(RunResult(c, ""))[0] for c in (0, 2, 5, 91, 124)] == [Status.PASS] + [Status.SKIP] * 4
    assert classify(RunResult(-1, "", error="boom"))[0] == Status.SKIP


def test_local_runner_end_to_end(tmp_path: Path):
    from culprit.gitops import changed_lines, first_parent_range, make_bundle, patch_for
    from culprit.runners.local_runner import LocalRunner

    repo = tmp_path / "r"
    repo.mkdir()

    def g(*a):
        return subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True, text=True).stdout.strip()

    g("init", "-q", "-b", "main"); g("config", "user.name", "T"); g("config", "user.email", "t@example.com")
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (repo / "tests").mkdir()
    (repo / "tests/test_calc.py").write_text("from calc import add\n\ndef test_small():\n    assert add(1, 1) == 2\n")
    g("add", "-A"); g("commit", "-q", "-m", "c1 good"); c1 = g("rev-parse", "HEAD")
    (repo / "README.md").write_text("hi\n"); g("add", "-A"); g("commit", "-q", "-m", "c2 docs")
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b if a < 10 else a + b + 1\n")
    g("add", "-A"); g("commit", "-q", "-m", "c3 bug"); c3 = g("rev-parse", "HEAD")
    (repo / "README.md").write_text("hello\n"); g("add", "-A"); g("commit", "-q", "-m", "c4 docs"); c4 = g("rev-parse", "HEAD")
    assert [c.subject for c in first_parent_range(repo, c1, c4)] == ["c2 docs", "c3 bug", "c4 docs"]

    test = "from calc import add\n\ndef test_big():\n    assert add(10, 5) == 15, add(10, 5)\n"
    fixed = "def add(a, b):\n    return a + b\n"

    async def run():
        r = LocalRunner(parallel=4)
        await r.prepare(make_bundle(repo, tmp_path / "b.bundle"), c4)
        try:
            probes = await asyncio.gather(*(r.probe(s, test, False, 60) for s in (c1, c3, c4)))
            verifies = await asyncio.gather(
                r.verify(c4, test, 60, patch_files={"calc.py": fixed}),
                r.verify(c4, test, 60, revert_sha=c3),
                r.verify(c4, test, 60),
            )
        finally:
            await r.close()
        return probes, verifies

    probes, (fix, revert, baseline) = asyncio.run(run())
    assert [classify(p)[0] for p in probes] == [Status.PASS, Status.FAIL, Status.FAIL]
    assert fix.passed and revert.passed
    assert baseline.repro_code == 1
    assert any("test_culprit_regression" in f for f in baseline.suite_failures)

    patch, err = patch_for(repo, c4, files={"calc.py": fixed}, extra={"tests/test_culprit_regression.py": test})
    assert err is None and "calc.py" in patch
    assert changed_lines(patch, exclude=("tests/test_culprit_regression.py",)) == 2
