"""Never approve an untestable candidate or upload an unsafe model path."""

import pytest

from culprit.fixer import candidate_files, passes_baseline
from culprit.runners.base import REGRESSION_TEST, VerifyResult


def test_existing_failures_allowed_but_new_failures_rejected():
    baseline = VerifyResult(1, 1, suite_failures={"tests/test_old.py::test_old", REGRESSION_TEST + "::test_bug"})
    assert passes_baseline(VerifyResult(0, 1, suite_failures={"tests/test_old.py::test_old"}), baseline)
    assert not passes_baseline(VerifyResult(0, 1, suite_failures={"tests/test_new.py::test_new"}), baseline)
    for code in (None, 2, 5, 90, 124):
        assert not passes_baseline(VerifyResult(0, code), baseline)
    assert not passes_baseline(VerifyResult(1, 0), baseline)
    assert not passes_baseline(VerifyResult(0, 1), baseline)


@pytest.mark.parametrize("path", ["../outside.py", "/outside.py", "C:/outside.py", ".git/config", ".GIT/config", "new_module.py"])
def test_reject_unsafe_or_disallowed_new_paths(path):
    with pytest.raises(ValueError):
        candidate_files({"files": [{"path": path, "content": "x"}]}, {"billing/money.py"})


def test_regression_path_is_dropped():
    assert candidate_files({"files": [
        {"path": REGRESSION_TEST, "content": "wrong"},
        {"path": "billing/money.py", "content": "fixed"},
    ]}, {"billing/money.py"}) == {"billing/money.py": "fixed"}
