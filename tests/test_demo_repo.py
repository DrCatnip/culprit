"""The demo is real history: a hidden regression, green suites, and a broken revert."""

import os
import subprocess
import sys

import pytest

from culprit.demo_repo import (
    CULPRIT_SUBJECT, DEMO_BUG, DEMO_REPRO_TEST, EXPORT_SUBJECT,
    MONEY_AFTER, MONEY_BEFORE, TAGS, build_demo_repo, is_demo_repo,
)
from culprit.gitops import file_at, files_touched, git, rev_parse


@pytest.fixture(scope="module")
def demo(tmp_path_factory):
    return build_demo_repo(tmp_path_factory.mktemp("demo-history") / "kirana-billing")


def run_pytest(repo, *args):
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONPATH=str(repo), PYTEST_DISABLE_PLUGIN_AUTOLOAD="1")
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-rfE", "--tb=short", "-p", "no:cacheprovider", *map(str, args)],
        cwd=repo, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )


def clone_demo(demo, target):
    subprocess.run(["git", "clone", "-q", "--no-hardlinks", str(demo), str(target)], check=True)
    return target


def test_deterministic_history(demo, tmp_path):
    again = build_demo_repo(tmp_path / "again")
    assert rev_parse(demo, "HEAD") == rev_parse(again, "HEAD")
    assert git(demo, "rev-list", "--reverse", "HEAD") == git(again, "rev-list", "--reverse", "HEAD")
    assert git(demo, "rev-list", "--count", "HEAD").strip() == "200"
    assert set(git(demo, "tag").split()) == set(TAGS.values())
    assert rev_parse(demo, "v1.5.0") == rev_parse(demo, "HEAD")
    assert is_demo_repo(demo)
    assert (demo.parent / "BUG.txt").read_text(encoding="utf-8").strip() == DEMO_BUG


def test_exact_milestones_and_unchanged_money(demo):
    commits = git(demo, "rev-list", "--reverse", "HEAD").splitlines()
    assert git(demo, "show", "-s", "--format=%s", commits[111]).strip() == CULPRIT_SUBJECT
    assert git(demo, "show", "-s", "--format=%an", commits[111]).strip() == "Arjun Mehta"
    assert git(demo, "show", "-s", "--format=%s", commits[117]).strip() == EXPORT_SUBJECT
    assert git(demo, "show", "-s", "--format=%an", commits[117]).strip() == "Kabir Shah"
    assert set(files_touched(demo, commits[111])) == {"billing/money.py", "tests/test_money.py"}
    assert file_at(demo, commits[110], "billing/money.py") == MONEY_BEFORE
    assert file_at(demo, commits[111], "billing/money.py") == MONEY_AFTER
    assert not git(demo, "log", "--format=%H", f"{commits[111]}..HEAD", "--", "billing/money.py", "tests/test_money.py").strip()
    assert len((demo / "billing/data/catalog.csv").read_text().splitlines()) >= 85


def test_suites_at_tags_tenths_and_regression_neighborhood(demo, tmp_path):
    repo = clone_demo(demo, tmp_path / "repo")
    commits = git(repo, "rev-list", "--reverse", "HEAD").splitlines()
    numbers = set(TAGS) | set(range(10, 201, 10)) | set(range(105, 126))
    for n in sorted(numbers):
        git(repo, "-c", "advice.detachedHead=false", "checkout", "-q", "--force", commits[n - 1])
        result = run_pytest(repo)
        assert result.returncode == 0, f"commit {n}\n{result.stdout}\n{result.stderr}"


def test_repro_and_linear_scan(demo, tmp_path):
    repo = clone_demo(demo, tmp_path / "repo")
    repro = tmp_path / "test_culprit_repro.py"
    repro.write_text(DEMO_REPRO_TEST, encoding="utf-8")
    git(repo, "checkout", "-q", "v1.2.0")
    assert run_pytest(repo, repro).returncode == 0
    git(repo, "checkout", "-q", "main")
    result = run_pytest(repo, repro)
    assert result.returncode == 1 and "AssertionError" in result.stdout
    commits = git(repo, "rev-list", "--reverse", "HEAD").splitlines()
    first_bad = None
    for sha in commits:
        git(repo, "-c", "advice.detachedHead=false", "checkout", "-q", "--force", sha)
        result = run_pytest(repo, repro)
        if result.returncode == 1 and "AssertionError" in result.stdout:
            first_bad = sha
            break
        assert result.returncode in (0, 2), result.stdout
    assert first_bad == commits[111]
    assert git(repo, "show", "-s", "--format=%s", first_bad).strip() == CULPRIT_SUBJECT


def test_revert_applies_but_breaks_export(demo, tmp_path):
    repo = clone_demo(demo, tmp_path / "repo")
    culprit = git(repo, "rev-list", "--reverse", "HEAD").splitlines()[111]
    git(repo, "-c", "user.name=Test", "-c", "user.email=test@kirana.example", "revert", "--no-commit", culprit)
    result = run_pytest(repo, "tests/test_export.py")
    assert result.returncode == 2 and "ImportError" in result.stdout and "to_paise" in result.stdout


def test_force_refuses_unrelated_directory(tmp_path):
    target = tmp_path / "unrelated"
    target.mkdir()
    (target / "keep.txt").write_text("keep")
    with pytest.raises(ValueError):
        build_demo_repo(target, force=True)
    assert (target / "keep.txt").read_text() == "keep"
